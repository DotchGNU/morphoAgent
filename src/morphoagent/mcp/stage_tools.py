"""Agent-facing wrappers of pipeline stages 0-3.

Every function here is a plain Python function with JSON-serialisable
arguments and return values, so it can be called directly (tests, notebooks)
or registered as a typed tool by :mod:`morphoagent.mcp.pipeline_server`. This
module does not import the MCP SDK.

Rules shared by all functions
-----------------------------
* Input images are only read, never written.
* Existing results are never replaced unless ``overwrite=True`` is passed; the
  pipeline then removes and recreates only the folder of that step.
* Each call returns the paths of the files it produced plus summary numbers
  read back from those files, so an agent can quote the numbers it reports.
* Stage tools 1-3 refuse to run (:class:`~morphoagent.records.DecisionRequiredError`)
  while a proposal of an earlier stage waits for the researcher's decision.

Stages
------
0 consultation  :func:`inspect_folder`
1 contrast      :func:`preview_enhance`
2 masks / cuts  :func:`run_segment`, :func:`cut_distributions`
3 shape modes   :func:`run_vampire`
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image

from ..pipeline.common import (
    MANIFEST,
    OutputExistsError,
    RunContext,
    condition_order,
    file_sha256,
    load_gray,
    package_versions,
    read_manifest,
    require_step,
    step_dir,
)
from ..pipeline.config import PRESET_NAMES, Config, ConfigError, InputConfig, load_config
from ..pipeline.selection import GROUP_FRAME, GROUP_KEPT, GROUP_SMALL, GROUP_TRIM, group_rule, image_trim_mask, largest_piece

LOG = logging.getLogger("morphoagent")

STAGES: dict[str, str] = {
    "inspect_folder": "0 consultation",
    "preview_enhance": "1 contrast",
    "run_segment": "2 masks and cuts",
    "cut_distributions": "2 masks and cuts",
    "run_vampire": "3 shape modes",
}
#: Protocol stage of each tool (docs/protocol.md). A tool refuses to run while a
#: proposal of an earlier stage is undecided (``agent.require_decisions``).
TOOL_STAGE: dict[str, str] = {
    "preview_enhance": "contrast",
    "run_segment": "segment",
    "cut_distributions": "cut",
    "run_vampire": "cluster",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _jsonable(o: Any) -> Any:
    """Recursively convert numpy / pandas / Path values into plain JSON types."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, float) and np.isnan(o):
        return None
    return o


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame rows as JSON-ready dicts (NaN -> None)."""
    return _jsonable(df.astype(object).where(pd.notna(df), None).to_dict(orient="records"))


def _load(config_path: str, tool: str | None = None) -> Config:
    """Load the configuration; for a stage tool, enforce the decision guard before anything is written."""
    from ..pipeline.common import setup_logging
    from ..records import require_decision

    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    if tool is not None:
        require_decision(cfg, TOOL_STAGE[tool])
    return cfg


def _ensure_load(cfg: Config) -> bool:
    """Run step 1 (load) when it has not been run yet; never replaces an existing inventory.

    Returns
    -------
    bool
        True when step 1 was run by this call.
    """
    if (step_dir(cfg, 1) / MANIFEST).is_file():
        return False
    from ..pipeline.steps_image import run_load

    run_load(cfg, RunContext(overwrite=False))
    return True


def _files(d: Path, pattern: str) -> list[str]:
    return sorted(str(p) for p in d.glob(pattern))


# --------------------------------------------------------------------------- #
# Stage 0 - inspect_folder
# --------------------------------------------------------------------------- #
def inspect_folder(
    path: str,
    name_regex: str | None = None,
    strip_prefix: str = "",
    strip_suffix: str = "",
    extensions: list[str] | None = None,
    read_pixels: bool = True,
) -> dict[str, Any]:
    """Describe an image folder without writing anything.

    Parameters
    ----------
    path : str
        Flat folder of single-channel bright-field images.
    name_regex : str, optional
        Regular expression with named groups ``condition`` and ``field``,
        applied to the file stem after stripping. Default: ``input.name_regex``
        of the default configuration.
    strip_prefix, strip_suffix : str
        Removed from the start / end of the stem before parsing.
    extensions : list of str, optional
        Image extensions (case-insensitive). Default: ``input.extensions``.
    read_pixels : bool
        When true every image is decoded to report its size, bit depth and
        grey-level range (0-255 after conversion to 8 bit) and to detect
        all-zero captures. When false only file names are parsed.

    Returns
    -------
    dict
        ``n_files``, ``n_matched``, ``unmatched_examples``, ``conditions``
        (images per condition), ``image_sizes`` (px), ``pixel_modes``,
        ``all_zero_files``, ``grey_levels`` (8-bit summary),
        ``config_snippet`` (an ``input``/``conditions`` block to start a
        configuration) and ``warnings``.
    """
    from ..pipeline.steps_image import parse_name

    defaults = InputConfig()
    src = Path(path).expanduser()
    if not src.is_dir():
        raise FileNotFoundError(f"not a folder: {src}")
    regex = name_regex or defaults.name_regex
    rx = re.compile(regex)
    if not {"condition", "field"} <= set(rx.groupindex):
        raise ValueError("name_regex must define the named groups 'condition' and 'field'")
    exts = {e.lower() for e in (extensions or defaults.extensions)}
    entries = sorted(p for p in src.iterdir() if p.is_file())
    images = [p for p in entries if p.suffix.lower() in exts]
    other = [p.name for p in entries if p.suffix.lower() not in exts]
    rows, unmatched = [], []
    for p in images:
        parsed = parse_name(p.stem, rx, strip_prefix, strip_suffix)
        if parsed is None:
            unmatched.append(p.name)
            continue
        cond, fld = parsed
        row: dict[str, Any] = {"file": p.name, "condition": cond, "field": fld, "image": f"{cond}_{fld}"}
        if read_pixels:
            with Image.open(p) as im:
                row["mode"] = im.mode
                row["width"], row["height"] = im.size
            g = load_gray(p)
            row["min"], row["max"], row["mean"] = int(g.min()), int(g.max()), float(g.mean())
        rows.append(row)
    warnings: list[str] = []
    if not images:
        warnings.append(f"no file with extension {sorted(exts)}")
    if unmatched:
        warnings.append(f"{len(unmatched)} file name(s) do not match name_regex after stripping prefix/suffix")
    t = pd.DataFrame(rows)
    out: dict[str, Any] = {
        "path": str(src),
        "name_regex": regex,
        "strip_prefix": strip_prefix,
        "strip_suffix": strip_suffix,
        "extensions": sorted(exts),
        "n_files": len(images),
        "n_other_files": len(other),
        "n_matched": len(rows),
        "n_unmatched": len(unmatched),
        "unmatched_examples": unmatched[:20],
        "conditions": [],
        "image_sizes": [],
        "pixel_modes": {},
        "all_zero_files": [],
        "duplicate_image_ids": [],
        "grey_levels": None,
        "warnings": warnings,
    }
    if len(t):
        order = condition_order(Config(), t.condition)
        dup = sorted(set(t.image[t.image.duplicated(keep=False)]))
        if dup:
            warnings.append(f"{len(dup)} duplicate image id(s) (condition_field)")
        out["duplicate_image_ids"] = dup
        skip = defaults.skip_max_at_or_below
        per = []
        for c in order:
            sub = t[t.condition == c]
            item = {"condition": c, "n_images": len(sub)}
            if read_pixels:
                item["n_all_zero"] = int((sub["max"] <= skip).sum())
            per.append(item)
        out["conditions"] = per
        if read_pixels:
            sizes = t.groupby(["width", "height"]).size().reset_index(name="n_images")
            out["image_sizes"] = _records(sizes)
            out["pixel_modes"] = _jsonable(t["mode"].value_counts().to_dict())
            out["all_zero_files"] = list(t.loc[t["max"] <= skip, "file"])
            ok = t[t["max"] > skip]
            if len(ok):
                out["grey_levels"] = {"min_of_min": int(ok["min"].min()), "max_of_max": int(ok["max"].max()),
                                      "mean_of_means": round(float(ok["mean"].mean()), 3),
                                      "sd_of_means": round(float(ok["mean"].std(ddof=0)), 3)}
            if len(sizes) > 1:
                warnings.append(f"{len(sizes)} different image sizes")
            if out["all_zero_files"]:
                warnings.append(f"{len(out['all_zero_files'])} all-zero capture(s) will be skipped by step 1")
        out["config_snippet"] = {
            "input": {"image_dir": str(src), "strip_prefix": strip_prefix, "strip_suffix": strip_suffix,
                      "name_regex": regex, "extensions": sorted(exts)},
            "conditions": {"order": order, "reference": None},
        }
    else:
        out["config_snippet"] = None
    return _jsonable(out)


# --------------------------------------------------------------------------- #
# Stage 1 - preview_enhance
# --------------------------------------------------------------------------- #
def preview_enhance(
    config_path: str,
    presets: list[str] | None = None,
    samples: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Step 2 (enhance-preview): every preset on sample images, contact sheet and metrics.

    Runs step 1 first when it has not been run. Enhancement is for display
    and review only; segmentation and measurements always use the raw image.

    Parameters
    ----------
    config_path : str
        YAML configuration file.
    presets : list of str, optional
        Presets to compare (default: ``enhance.preview_presets``).
    samples : list of str, optional
        Image ids ``<condition>_<field>`` (default: ``enhance.samples`` or the
        first image of the first conditions).
    overwrite : bool
        Replace an existing ``02_enhance-preview`` folder.

    Returns
    -------
    dict
        ``step_dir``, ``contact_sheet``, ``preview_metrics_csv``, ``images``,
        ``ran_load``, ``default_preset`` and ``per_preset``: mean
        ``edge_strength`` (higher = crisper), ``flat_noise`` (grey levels,
        lower = cleaner background) and ``p50`` (grey levels) per preset.
    """
    from ..pipeline.steps_image import run_enhance_preview

    cfg = _load(config_path, "preview_enhance")
    if presets:
        bad = [p for p in presets if p not in PRESET_NAMES]
        if bad:
            raise ConfigError(f"unknown preset(s) {bad}; choose from {list(PRESET_NAMES)}")
        cfg.enhance.preview_presets = list(presets)
    if samples:
        cfg.enhance.samples = list(samples)
    ran_load = _ensure_load(cfg)
    run_enhance_preview(cfg, RunContext(overwrite=overwrite))
    d = step_dir(cfg, 2)
    met = pd.read_csv(d / "preview_metrics.csv")
    per = (met.groupby("preset", sort=False)[["edge_strength", "flat_noise", "p50"]].mean().round(4).reset_index())
    return _jsonable({
        "stage": STAGES["preview_enhance"],
        "step_dir": d,
        "contact_sheet": d / "contact_sheet.png",
        "contact_sheet_source": d / "source_contact_sheet.csv",
        "preview_metrics_csv": d / "preview_metrics.csv",
        "manifest": d / MANIFEST,
        "images": _files(d / "images", "*.png"),
        "ran_load": ran_load,
        "sample_images": list(dict.fromkeys(met.image)),
        "default_preset": cfg.enhance.preset,
        "per_preset": _records(per),
    })


# --------------------------------------------------------------------------- #
# Stage 2 - run_segment
# --------------------------------------------------------------------------- #
def run_segment(config_path: str, mode: str = "tune", overwrite: bool = False) -> dict[str, Any]:
    """Cellpose-SAM segmentation: parameter grid on samples (step 4) or all images (step 5).

    Parameters
    ----------
    config_path : str
        YAML configuration file (``segment`` section).
    mode : {"tune", "all"}
        ``tune`` runs step 4 (``segment.tune`` grid on sample images, outline
        montages); ``all`` runs step 5 with the ``segment`` setting.
    overwrite : bool
        Replace an existing step folder.

    Returns
    -------
    dict
        For ``tune``: ``montages``, ``tune_counts_csv`` and ``per_setting``
        (mean objects per sample image, all and with area >= ``area_min_px``).
        For ``all``: ``labels_dir``, ``objects_csv``, ``n_images``,
        ``n_objects`` and ``objects_per_condition``. Both include
        ``gpu_used`` and ``ran_load``.
    """
    from ..pipeline.steps_image import run_segment as _run_segment
    from ..pipeline.steps_image import run_segment_tune

    if mode not in ("tune", "all"):
        raise ValueError("mode must be 'tune' or 'all'")
    cfg = _load(config_path, "run_segment")
    ran_load = _ensure_load(cfg)
    ctx = RunContext(overwrite=overwrite)
    try:
        (run_segment_tune if mode == "tune" else _run_segment)(cfg, ctx)
    except ImportError as exc:
        raise RuntimeError(f"{exc}. Install the segmentation extra: pip install 'morphoagent[segment]'") from exc
    if mode == "tune":
        d = step_dir(cfg, 4)
        m = read_manifest(d)
        counts = pd.read_csv(d / "tune_counts.csv")
        per = counts.groupby(["setting", "downscale", "flow_threshold", "cellprob_threshold", "is_default"],
                             dropna=False, sort=False).agg(
            diameter=("diameter", "first"), mean_n_objects=("n_objects", "mean"),
            mean_n_objects_area_ge_min=("n_objects_area_ge_min", "mean")).round(2).reset_index()
        return _jsonable({
            "stage": STAGES["run_segment"], "mode": mode, "step_dir": d, "manifest": d / MANIFEST,
            "settings_csv": d / "settings.csv", "tune_counts_csv": d / "tune_counts.csv",
            "montages": _files(d / "montages", "*.png"), "sample_images": m["counts_in"]["sample_images"],
            "area_min_px": cfg.select.area_min_px, "per_setting": _records(per), "gpu_used": m.get("gpu_used"),
            "ran_load": ran_load,
        })
    d = step_dir(cfg, 5)
    m = read_manifest(d)
    return _jsonable({
        "stage": STAGES["run_segment"], "mode": mode, "step_dir": d, "manifest": d / MANIFEST,
        "labels_dir": d / "labels", "objects_csv": d / "objects.csv", "images_csv": d / "images.csv",
        "n_images": m["counts_out"]["label_tifs"], "n_objects": m["counts_out"]["objects"],
        "objects_per_condition": m["counts_out"]["objects_per_condition"],
        "parameters": {k: m["parameters"][k] for k in ("model", "flow_threshold", "cellprob_threshold", "diameter",
                                                        "downscale")},
        "gpu_used": m.get("gpu_used"), "ran_load": ran_load,
    })


# --------------------------------------------------------------------------- #
# Stage 2 - cut_distributions
# --------------------------------------------------------------------------- #
def _objects_for_cuts(cfg: Config) -> tuple[pd.DataFrame, int]:
    """Objects (after largest-piece cleaning and image trim) with area and frame contact.

    Uses the objects table of step 6 when it exists; otherwise reads the label
    images of step 5 and applies the same largest-piece and image-trim rules
    in memory (nothing is written).
    """
    from ..pipeline.config import shape_grid_factor
    from ..pipeline.segmentation import measure_objects

    sel = step_dir(cfg, 6)
    if (sel / MANIFEST).is_file():
        o = pd.read_csv(sel / "objects.csv")
        return o[o.group != GROUP_TRIM].reset_index(drop=True), 6
    import tifffile

    seg = require_step(cfg, 5)
    images = pd.read_csv(seg / "images.csv")
    sc = cfg.select
    parts = []
    for r in images.itertuples():
        lab = tifffile.imread(seg / "labels" / f"{r.image}_labels.tif")
        removed: dict[int, int] = {}
        if sc.largest_piece:
            lab, removed = largest_piece(lab, sc.connectivity)
        m = measure_objects(lab, None, shape_grid_factor(cfg), set(removed))
        m.insert(0, "field", r.field)
        m.insert(0, "condition", r.condition)
        m.insert(0, "image", r.image)
        parts.append(m)
    o = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(o) and sc.image_trim_fraction > 0:
        keep = image_trim_mask(o, sc.image_trim_fraction, sc.image_trim_features, sc.image_trim_log_area,
                               sc.image_trim_min_images)
        o = o[keep].reset_index(drop=True)
    return o, 5


def cut_table(objects: pd.DataFrame, area_candidates: list[float], contact_candidates: list[float],
              conditions: list[str]) -> pd.DataFrame:
    """Remaining objects per condition for every (area minimum, frame-contact maximum) pair.

    The step-6 rule is applied in its order: objects with ``area_px`` below
    the area minimum are dropped first, then objects with more than the
    frame-contact maximum pixels on the image frame.

    Parameters
    ----------
    objects : pandas.DataFrame
        Columns ``condition``, ``image``, ``area_px`` (px^2) and
        ``border_contact_px`` (px).
    area_candidates : list of float
        Area minima (px^2).
    contact_candidates : list of float
        Frame-contact maxima (px).
    conditions : list of str
        Conditions in report order.

    Returns
    -------
    pandas.DataFrame
        One row per (area_min_px, frame_contact_px, condition) with
        ``n_objects_in``, ``n_below_area_min``, ``n_frame_contact``,
        ``n_kept``, ``n_images`` and ``n_images_without_kept``.
    """
    rows = []
    for a in area_candidates:
        for c in contact_candidates:
            g = group_rule(objects.area_px, objects.border_contact_px, a, c)
            for cond in conditions:
                m = (objects.condition == cond).to_numpy()
                gi = g[m]
                imgs = objects.image[m]
                kept_imgs = set(imgs[gi == GROUP_KEPT])
                n_img = int(imgs.nunique())
                rows.append({"area_min_px": float(a), "frame_contact_px": float(c), "condition": cond,
                             "n_objects_in": int(m.sum()), "n_below_area_min": int((gi == GROUP_SMALL).sum()),
                             "n_frame_contact": int((gi == GROUP_FRAME).sum()), "n_kept": int((gi == GROUP_KEPT).sum()),
                             "n_images": n_img, "n_images_without_kept": n_img - len(kept_imgs)})
    return pd.DataFrame(rows)


def _cut_figure(objects: pd.DataFrame, areas: list[float], contacts: list[float], current: tuple[float, float],
                bins: int, cfg: Config) -> tuple[Any, pd.DataFrame]:
    """Two histograms (log10 area, frame contact) with candidate cut-off lines and their source table."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..pipeline.plotting import apply_style

    apply_style(cfg.plot)
    lw = cfg.stats.box.line_width_pt
    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.6))
    src = []
    n = len(objects)
    la = np.log10(objects.area_px.clip(lower=1).to_numpy(float))
    contact = objects.border_contact_px.to_numpy(float)
    panels = [("log10_area_px", la, [np.log10(max(a, 1.0)) for a in areas], np.log10(max(current[0], 1.0)),
               "log10 area (px$^2$)"),
              ("border_contact_px", contact, list(contacts), current[1], "pixels on the image frame")]
    for ax, (name, values, cuts, cur, xlabel) in zip(axes, panels, strict=True):
        hi = max(float(values.max()) if n else 1.0, max(cuts) if cuts else 1.0)
        lo = min(float(values.min()) if n else 0.0, min(cuts) if cuts else 0.0)
        edges = np.linspace(lo, hi if hi > lo else lo + 1, bins + 1)
        counts, _ = np.histogram(values, edges)
        ax.bar(edges[:-1], counts, width=np.diff(edges), align="edge", color="#B0B0B0", edgecolor="none",
               label=f"objects (n={n})")
        for e0, e1, k in zip(edges[:-1], edges[1:], counts, strict=True):
            src.append({"panel": name, "row_type": "bin", "x_left": e0, "x_right": e1, "count": int(k),
                        "is_current": None, "n_objects": n})
        for x in cuts:
            is_cur = bool(np.isclose(x, cur))
            ax.axvline(x, color="#D62728" if is_cur else "#555555", lw=1.2 if is_cur else lw,
                       ls="-" if is_cur else "--")
            src.append({"panel": name, "row_type": "cutoff", "x_left": x, "x_right": x, "count": None,
                        "is_current": is_cur, "n_objects": n})
        ax.set_xlabel(xlabel)
        ax.set_ylabel("objects")
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    return fig, pd.DataFrame(src)


def _agent_dir(cfg: Config, tool: str, params: dict[str, Any], overwrite: bool) -> Path:
    """``<output_dir>/<agent.output_subdir>/<tool>_<sha256(params)[:10]>``; refuses to replace without overwrite."""
    key = hashlib.sha256(json.dumps(_jsonable(params), sort_keys=True).encode()).hexdigest()[:10]
    root = Path(cfg.output_dir) / cfg.agent.output_subdir
    d = root / f"{tool}_{key}"
    if d.exists() and any(d.iterdir()):
        if not overwrite:
            raise OutputExistsError(f"{d} already contains results; pass overwrite=True to replace them")
        import shutil

        if d.resolve().parent != root.resolve():
            raise RuntimeError(f"refusing to remove unexpected directory {d}")
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_agent_manifest(d: Path, cfg: Config, tool: str, parameters: dict[str, Any], counts_in: dict[str, Any],
                          counts_out: dict[str, Any]) -> Path:
    m = {"tool": tool, "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
         "versions": package_versions(), "config_file": cfg.config_path, "config_sha256": file_sha256(cfg.config_path),
         "parameters": parameters, "counts_in": counts_in, "counts_out": counts_out}
    p = d / MANIFEST
    p.write_text(json.dumps(_jsonable(m), indent=2), encoding="utf-8")
    return p


def cut_distributions(
    config_path: str,
    area_min_px: list[float] | None = None,
    frame_contact_px: list[float] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Remaining cells per condition for candidate area and frame-contact cut-offs.

    Reads the segmentation (step 6 objects when available, otherwise the
    step-5 label images with the largest-piece rule applied in memory),
    applies every candidate pair with the step-6 rule and writes a table and
    a histogram figure to ``<output_dir>/<agent.output_subdir>/cut-distributions_<hash>/``.
    The configuration is not changed; the chosen values must be written to
    ``select.area_min_px`` / ``select.frame_contact_px`` before step 6 is run.

    Parameters
    ----------
    config_path : str
        YAML configuration file.
    area_min_px : list of float, optional
        Area minima to test (px^2, full-resolution image). Default: factors
        ``agent.cut.area_min_factors`` x ``select.area_min_px``.
    frame_contact_px : list of float, optional
        Frame-contact maxima to test (px). Default: factors
        ``agent.cut.frame_contact_factors`` x ``select.frame_contact_px``.
    overwrite : bool
        Replace an existing result folder for the same parameters.

    Returns
    -------
    dict
        ``out_dir``, ``files``, ``source_step`` (5 or 6), ``n_objects``,
        ``current`` cut-offs, the candidate lists, ``rows`` (one per
        candidate pair and condition, see :func:`cut_table`) and
        ``totals`` (kept cells over all conditions per candidate pair).
    """
    from ..pipeline.plotting import save_figure

    cfg = _load(config_path, "cut_distributions")
    sc, cc = cfg.select, cfg.agent.cut
    current = (float(sc.area_min_px), float(sc.frame_contact_px))
    areas = sorted({float(a) for a in (area_min_px or [f * current[0] for f in cc.area_min_factors])} | {current[0]})
    contacts = sorted({float(c) for c in (frame_contact_px or [f * current[1] for f in cc.frame_contact_factors])}
                      | {current[1]})
    if any(a < 0 for a in areas) or any(c < 0 for c in contacts):
        raise ValueError("cut-off candidates must be >= 0")
    objects, source_step = _objects_for_cuts(cfg)
    if not len(objects):
        raise RuntimeError("no segmented objects found")
    conds = condition_order(cfg, objects.condition)
    params = {"area_min_px": areas, "frame_contact_px": contacts, "source_step": source_step,
              "largest_piece": sc.largest_piece, "connectivity": sc.connectivity,
              "image_trim_fraction": sc.image_trim_fraction, "histogram_bins": cc.histogram_bins,
              "rule_order": "area minimum first, then frame contact (as step 6)"}
    d = _agent_dir(cfg, "cut-distributions", params, overwrite)
    table = cut_table(objects, areas, contacts, conds)
    table["is_current"] = np.isclose(table.area_min_px, current[0]) & np.isclose(table.frame_contact_px, current[1])
    table.to_csv(d / "cut_table.csv", index=False)
    pivot = table.pivot_table(index=["area_min_px", "frame_contact_px"], columns="condition", values="n_kept",
                              aggfunc="sum")[conds]
    pivot["all_conditions"] = pivot.sum(axis=1)
    pivot.reset_index().to_csv(d / "cut_kept_pivot.csv", index=False)
    fig, srcdf = _cut_figure(objects, areas, contacts, current, cc.histogram_bins, cfg)
    figs = save_figure(fig, d, "cut_histograms", srcdf, cfg.plot.dpi)
    totals = table.groupby(["area_min_px", "frame_contact_px"], as_index=False)[["n_kept", "n_objects_in"]].sum()
    _write_agent_manifest(d, cfg, "cut_distributions", params,
                          {"objects": len(objects), "images": int(objects.image.nunique()), "conditions": conds},
                          {"candidate_pairs": len(areas) * len(contacts), "rows": len(table)})
    return _jsonable({
        "stage": STAGES["cut_distributions"],
        "out_dir": d,
        "files": [d / "cut_table.csv", d / "cut_kept_pivot.csv", *figs, d / MANIFEST],
        "source_step": source_step,
        "n_objects": len(objects),
        "n_images": int(objects.image.nunique()),
        "conditions": conds,
        "current": {"area_min_px": current[0], "frame_contact_px": current[1]},
        "area_min_px_candidates": areas,
        "frame_contact_px_candidates": contacts,
        "rows": _records(table),
        "totals": _records(totals),
    })


# --------------------------------------------------------------------------- #
# Stage 3 - run_vampire
# --------------------------------------------------------------------------- #
def run_vampire(config_path: str, k: list[int] | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Step 9: VAMPIRE shape modes on the kept cells, elbow analysis and per-K summaries.

    Uses the curated tables of step 8 when that step was applied, otherwise
    those of step 6. VAMPIRE runs in the interpreter given by
    ``vampire.python`` from the checkout in ``vampire.src_path``.

    Parameters
    ----------
    config_path : str
        YAML configuration file.
    k : list of int, optional
        Numbers of shape modes to fit (each >= 2). Default: ``vampire.k``.
    overwrite : bool
        Replace an existing ``09_vampire`` folder.

    Returns
    -------
    dict
        ``step_dir``, ``elbow`` (suggested K by second difference and by
        kneedle, inertia table path), ``per_k`` (objects per display mode,
        objects dropped by VAMPIRE, raw-to-display mode mapping),
        ``figures`` and ``tables``.
    """
    from ..pipeline.vampire import run_vampire as _run_vampire

    cfg = _load(config_path, "run_vampire")
    if k is not None:
        if not k or any(int(x) < 2 for x in k):
            raise ValueError("k must be a non-empty list of integers >= 2")
        cfg.vampire.k = [int(x) for x in k]
    _run_vampire(cfg, RunContext(overwrite=overwrite))
    d = step_dir(cfg, 9)
    m = read_manifest(d)
    elbow = json.loads((d / "elbow_suggestions.json").read_text())
    return _jsonable({
        "stage": STAGES["run_vampire"],
        "step_dir": d,
        "manifest": d / MANIFEST,
        "k": cfg.vampire.k,
        "input_tables_from_step": m["parameters"]["input_tables_from_step"],
        "n_cells": m["counts_in"]["cells"],
        "elbow": {**elbow, "inertia_csv": d / "elbow_inertia.csv"},
        "per_k": m["counts_out"]["per_k"],
        "figures": _files(d / "figures", "*.png"),
        "tables": _files(d, "*.csv"),
        "vampire": dataclasses.asdict(cfg.vampire),
    })
