"""Collect pipeline outputs into the JSON document embedded in the review page.

Everything the page needs is gathered here; the page itself never reads files.
Images are embedded as base64 JPEG and label maps as base64 PNG whose red
channel holds the low byte and green channel the high byte of the label
number (``label = R + 256 * G``), so the browser can decode them into a
``Uint16Array`` through a canvas without any library.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import hashlib
import inspect
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import tifffile
from PIL import Image

from .. import __version__
from ..common import LOG, MANIFEST, condition_order, label_path, load_gray, progress, read_inventory, step_dir
from ..config import Config
from ..overlay import grid_sheet, inside_point
from ..presets import get_preset, preview_metrics
from ..selection import GROUP_FRAME, GROUP_KEPT

SCHEMA = "morphoagent-ui-data/1"
LABEL_ENCODING = "png-rgb: label = R + 256*G"


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #
def data_uri(mime: str, payload: bytes) -> str:
    """Return a ``data:`` URI with base64 payload."""
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def fit_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    """(width, height) scaled down to at most ``max_width`` px wide, aspect kept (never enlarged)."""
    if width <= max_width:
        return int(width), int(height)
    return int(max_width), max(1, int(round(height * max_width / width)))


def encode_jpeg(img: np.ndarray, size: tuple[int, int] | None, quality: int) -> bytes:
    """JPEG bytes of a uint8 grey or RGB image, Lanczos-resized to ``size`` = (width, height) if given."""
    pil = Image.fromarray(img)
    if size is not None and pil.size != tuple(size):
        pil = pil.resize(tuple(size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=int(quality), optimize=True)
    return buf.getvalue()


def resize_labels(labels: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour resize of a label image to ``size`` = (width, height); returns int32."""
    lab = labels.astype(np.int32)
    if (lab.shape[1], lab.shape[0]) == tuple(size):
        return lab
    return np.array(Image.fromarray(lab, mode="I").resize(tuple(size), Image.Resampling.NEAREST)).astype(np.int32)


def encode_label_png(labels: np.ndarray) -> bytes:
    """PNG bytes of a label image (< 65536 labels) as RGB: R = low byte, G = high byte, B = 0."""
    lab = labels.astype(np.int64)
    if lab.min() < 0 or lab.max() > 65535:
        raise ValueError("label values must be in 0..65535 to be embedded")
    rgb = np.zeros((*lab.shape, 3), np.uint8)
    rgb[..., 0] = (lab & 255).astype(np.uint8)
    rgb[..., 1] = (lab >> 8).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def decode_label_png(payload: bytes) -> np.ndarray:
    """Inverse of :func:`encode_label_png` (used by tests)."""
    rgb = np.array(Image.open(io.BytesIO(payload)).convert("RGB")).astype(np.int32)
    return rgb[..., 0] + 256 * rgb[..., 1]


def _clean(v: Any) -> Any:
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


# --------------------------------------------------------------------------- #
# Tab 1 - enhancement
# --------------------------------------------------------------------------- #
def _figure_default_target(info: str, fallback: float) -> float:
    try:
        d = json.loads(info)
        return round(float(d["median_before_shift"]) + float(d["median_shift"]), 3)
    except (ValueError, KeyError, TypeError):
        return fallback


def collect_enhance(cfg: Config) -> dict[str, Any]:
    """Preset previews of step 2 plus precomputed ``figure-default`` median-target variants.

    Returns
    -------
    dict
        ``{"available": bool, "reason": str, "current": {...}, "samples": [...]}``.
        Each sample has ``before`` (the unenhanced grey image) and ``variants``
        (``preset``, ``median_target`` or ``None``, ``uri``, ``metrics``,
        ``is_current``).
    """
    ec, u = cfg.enhance, cfg.ui
    current = {"preset": ec.preset, "median_target": ec.figure_default.median_target}
    d2 = step_dir(cfg, 2)
    if not (d2 / MANIFEST).is_file():
        return {"available": False, "reason": "step 2 (enhance-preview) has not been run", "current": current, "samples": []}
    met = pd.read_csv(d2 / "preview_metrics.csv")
    inv = read_inventory(cfg).set_index("image")
    samples = []
    for im in progress(list(dict.fromkeys(met.image)), "ui: enhancement previews"):
        rows = met[met.image == im]
        gray = load_gray(inv.loc[im, "path"])
        size = fit_size(gray.shape[1], gray.shape[0], u.preview_width_px)
        variants: list[dict[str, Any]] = []
        seen_targets: list[float] = []
        for r in rows.itertuples():
            f = d2 / "images" / f"{im}__{r.preset}.png"
            if not f.is_file():
                continue
            img = np.array(Image.open(f).convert("L"))
            target = _figure_default_target(r.info, ec.figure_default.median_target) if r.preset == "figure-default" else None
            if target is not None:
                seen_targets.append(target)
            variants.append({
                "preset": r.preset, "median_target": target, "source": "step 2",
                "uri": data_uri("image/jpeg", encode_jpeg(img, size, u.jpeg_quality)),
                "metrics": {k: _clean(getattr(r, k)) for k in ("edge_strength", "flat_noise", "p1", "p50", "p99")},
            })
        if "figure-default" in set(rows.preset):
            for t in u.median_target_grid:
                if any(abs(float(t) - s) < 1e-6 for s in seen_targets):
                    continue
                params = dataclasses.replace(ec.figure_default, median_target=float(t))
                out, _ = get_preset("figure-default", dataclasses.replace(ec, figure_default=params))(gray)
                m = preview_metrics(out, ec)
                variants.append({
                    "preset": "figure-default", "median_target": float(t), "source": "ui build",
                    "uri": data_uri("image/jpeg", encode_jpeg(out, size, u.jpeg_quality)),
                    "metrics": {k: _clean(m[k]) for k in ("edge_strength", "flat_noise", "p1", "p50", "p99")},
                })
        variants.sort(key=lambda v: (v["preset"] != "figure-default", v["preset"], v["median_target"] or 0))
        for v in variants:
            v["is_current"] = v["preset"] == ec.preset and (
                v["preset"] != "figure-default" or abs((v["median_target"] or 0) - ec.figure_default.median_target) < 1e-6)
        samples.append({"image": im, "condition": str(inv.loc[im, "condition"]),
                        "before": data_uri("image/jpeg", encode_jpeg(gray, size, u.jpeg_quality)), "variants": variants})
    return {"available": True, "reason": "", "current": current, "samples": samples}


# --------------------------------------------------------------------------- #
# Tab 2 - segmentation settings
# --------------------------------------------------------------------------- #
def _montage_geometry() -> tuple[int, int]:
    params = inspect.signature(grid_sheet).parameters
    return int(params["caption_px"].default), int(params["pad"].default)


def crop_montage_panels(montage: np.ndarray, n_rows: int, n_cols: int) -> list[list[np.ndarray]]:
    """Cut a step-4 montage (``overlay.grid_sheet`` without column titles) back into its panels.

    Parameters
    ----------
    montage : numpy.ndarray
        RGB montage image.
    n_rows, n_cols : int
        Grid size (from the montage's ``source_*.csv``).

    Returns
    -------
    list of list of numpy.ndarray
        Panels without their captions, ``[row][col]``.
    """
    cap, pad = _montage_geometry()
    h, w = montage.shape[:2]
    pw = (w - pad) // n_cols - pad
    cell_h = (h - pad) // n_rows
    ph = cell_h - 2 * cap - 3 * pad
    if pw <= 0 or ph <= 0:
        raise ValueError("montage size does not match the expected grid")
    out = []
    for i in range(n_rows):
        y0 = i * cell_h + pad
        out.append([montage[y0 : y0 + ph, pad + j * (pw + pad) : pad + j * (pw + pad) + pw] for j in range(n_cols)])
    return out


def collect_segment(cfg: Config) -> dict[str, Any]:
    """Step-4 outline panels per setting and sample image, with object counts."""
    s = cfg.segment
    current = {"flow_threshold": s.flow_threshold, "cellprob_threshold": s.cellprob_threshold,
               "diameter": s.diameter, "downscale": s.downscale}
    d4 = step_dir(cfg, 4)
    if not (d4 / MANIFEST).is_file():
        return {"available": False, "reason": "step 4 (segment-tune) has not been run", "current": current,
                "settings": [], "samples": []}
    settings = pd.read_csv(d4 / "settings.csv")
    settings_out = [{k: _clean(v) for k, v in r.items()} for r in settings.to_dict("records")]
    for r in settings_out:
        if r.get("diameter") is None or (isinstance(r["diameter"], float) and np.isnan(r["diameter"])):
            r["diameter"] = None
    inv = read_inventory(cfg).set_index("image")
    samples = []
    for src in sorted((d4 / "montages").glob("source_*_tune-montage.csv")):
        im = src.name[len("source_"): -len("_tune-montage.csv")]
        png = d4 / "montages" / f"{im}_tune-montage.png"
        if not png.is_file():
            continue
        lay = pd.read_csv(src)
        grid = crop_montage_panels(np.array(Image.open(png).convert("RGB")), int(lay.row.max()) + 1, int(lay.col.max()) + 1)
        panels = []
        for r in lay.itertuples():
            p = grid[int(r.row)][int(r.col)]
            panels.append({"setting": r.setting, "row": int(r.row), "col": int(r.col),
                           "n_objects": int(r.n_objects), "n_objects_area_ge_min": int(r.n_objects_area_ge_min),
                           "uri": data_uri("image/jpeg", encode_jpeg(p, None, cfg.ui.jpeg_quality))})
        samples.append({"image": im, "condition": str(inv.loc[im, "condition"]) if im in inv.index else "", "panels": panels,
                        "n_cols": int(lay.col.max()) + 1})
    order = condition_order(cfg, [s["condition"] for s in samples])
    samples.sort(key=lambda s: (order.index(s["condition"]), s["image"]))
    return {"available": bool(samples), "reason": "" if samples else "no montages found in step 4",
            "current": current, "settings": settings_out, "samples": samples}


# --------------------------------------------------------------------------- #
# Tab 3 - mask review
# --------------------------------------------------------------------------- #
def _background(cfg: Config, image: str, raw_path: str, size: tuple[int, int]) -> np.ndarray:
    p = step_dir(cfg, 3) / "images" / f"{image}_enhanced.png"
    if cfg.review.background == "enhanced" and p.is_file():
        return np.array(Image.open(p).convert("L").resize(size, Image.Resampling.LANCZOS))
    return np.array(Image.fromarray(load_gray(raw_path)).resize(size, Image.Resampling.LANCZOS))


def field_objects(objects: pd.DataFrame, labels: np.ndarray, scale: float) -> list[dict[str, Any]]:
    """Compact per-object table of one field (label, area, frame contact, groups, shape, number position).

    ``x``/``y`` are the deepest inside point of the full-resolution mask,
    multiplied by ``scale`` (embedded px per full-resolution px).
    """
    out = []
    for r in objects.sort_values("label").itertuples():
        m = labels == int(r.label)
        if m.any():
            x, y = inside_point(m)
        else:
            x, y = float(r.centroid_x), float(r.centroid_y)
        out.append({
            "label": int(r.label), "area_px": _clean(r.area_px), "border_contact_px": _clean(r.border_contact_px),
            "group": str(r.group), "group_rule": str(r.group_rule), "aspect_ratio": _clean(r.aspect_ratio),
            "solidity": _clean(r.solidity), "form_factor": _clean(r.form_factor),
            "x": round(x * scale, 1), "y": round(y * scale, 1),
        })
    return out


def collect_fields(cfg: Config, images: list[str] | None = None) -> list[dict[str, Any]]:
    """Background image, label map and object table of every field of step 6.

    Parameters
    ----------
    cfg : Config
        Configuration (``ui.max_width``, ``ui.jpeg_quality``, ``review.background``).
    images : list of str, optional
        Restrict to these image ids (default: all).
    """
    d6 = step_dir(cfg, 6)
    if not (d6 / MANIFEST).is_file():
        LOG.info("ui: step 6 (select) has not been run; the mask-review tab is empty")
        return []
    fields = pd.read_csv(d6 / "fields.csv")
    objects = pd.read_csv(d6 / "objects.csv")
    inv = read_inventory(cfg).set_index("image")
    order = condition_order(cfg, fields.condition)
    fields["_o"] = fields.condition.map(order.index)
    fields = fields.sort_values(["_o", "field"]).drop(columns="_o")
    if images is not None:
        fields = fields[fields.image.isin(images)]
    out = []
    for f in progress(list(fields.itertuples()), "ui: fields"):
        lab = tifffile.imread(label_path(cfg, f.label_tif)).astype(np.int32)
        h, w = lab.shape
        size = fit_size(w, h, cfg.ui.max_width)
        scale = size[0] / w
        small = resize_labels(lab, size)
        o = objects[objects.image == f.image]
        out.append({
            "image": f.image, "condition": str(f.condition), "field": str(f.field),
            "full_width": int(w), "full_height": int(h), "width": size[0], "height": size[1], "scale": round(scale, 6),
            "image_trimmed": bool(f.image_trimmed),
            "n_kept": int((o.group == GROUP_KEPT).sum()), "n_frame_contact": int((o.group_rule == GROUP_FRAME).sum()),
            "bg": data_uri("image/jpeg", encode_jpeg(_background(cfg, f.image, inv.loc[f.image, "path"], size), None,
                                                     cfg.ui.jpeg_quality)),
            "labels": data_uri("image/png", encode_label_png(small)),
            "objects": field_objects(o, lab, scale),
        })
    return out


# --------------------------------------------------------------------------- #
# Whole document
# --------------------------------------------------------------------------- #
def dataset_id(fields: list[dict[str, Any]], output_dir: str) -> str:
    """Short stable id of a dataset (folder name, image ids, label counts); keys the browser's local storage."""
    h = hashlib.sha256(Path(output_dir).name.encode())
    for f in fields:
        h.update(f"{f['image']}:{len(f['objects'])};".encode())
    return h.hexdigest()[:16]


def build_dataset(cfg: Config, live: bool = False) -> dict[str, Any]:
    """Assemble the JSON document embedded in the page.

    Parameters
    ----------
    cfg : Config
        Loaded configuration. Tabs whose pipeline step has not been run
        (2, 4, 6) are shown with a note; the Decisions tab needs no step.
    live : bool
        True when served by ``morphoagent ui serve`` (enables autosave, live
        enhancement and, if configured, the assistant).

    Returns
    -------
    dict
        JSON-serialisable document; no absolute file paths are included.
    """
    fields = collect_fields(cfg)
    order = condition_order(cfg, [f["condition"] for f in fields])
    u = cfg.ui
    doc = {
        "schema": SCHEMA,
        "morphoagent_version": __version__,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "dataset_id": dataset_id(fields, cfg.output_dir),
        "dataset_name": Path(cfg.output_dir).name,
        "config_name": Path(cfg.config_path).name if cfg.config_path else "config.yaml",
        "conditions": order,
        "live": bool(live),
        "label_encoding": LABEL_ENCODING,
        "select": {"area_min_px": cfg.select.area_min_px, "frame_contact_px": cfg.select.frame_contact_px,
                   "connectivity": cfg.curate.connectivity},
        "review": {"kept_color": cfg.review.kept_color, "frame_contact_color": cfg.review.frame_contact_color},
        "sliders": {"median_target": u.median_target_range, "clahe_clip": u.clahe_clip_range,
                    "unsharp_amount": u.unsharp_amount_range},
        "llm": {"enabled": bool(live and u.llm.enabled), "provider": u.llm.provider,
                "endpoint_host": _llm_destination(u.llm.provider, u.llm.endpoint) if live and u.llm.enabled else None},
        "enhance": collect_enhance(cfg),
        "segment": collect_segment(cfg),
        "fields": fields,
        "fields_reason": "" if fields else "step 6 (select) has not been run yet",
        "decisions": collect_decisions(cfg),
    }
    LOG.info("ui: %d field(s), %d enhancement sample(s), %d segmentation sample(s)", len(fields),
             len(doc["enhance"]["samples"]), len(doc["segment"]["samples"]))
    return doc


def collect_decisions(cfg: Config) -> dict[str, Any]:
    """Proposals with their decision, hash and figure thumbnails for the Decisions tab.

    Returns
    -------
    dict
        ``proposals`` (see :func:`morphoagent.records.protocol.list_proposals`;
        each option gains ``thumbs``: JPEG data URIs of its figures),
        ``stages``, ``require_decisions``, ``reference_urls`` and the card
        table limits ``table_max_rows`` / ``table_max_columns``
        (``ui.card_table_max_rows`` / ``ui.card_table_max_columns``). No absolute
        path is included: ``figure_origins`` and the provenance paths are
        removed, and every other text (provenance parameters, rationale,
        option texts and tables, evidence, decision comment) passes through
        :func:`morphoagent.records.report.redact_paths` (paths inside the
        configuration folder become relative, others ``<path>/<name>``).
        The proposal hash is that of the file on disk, so decisions still match.
    """
    from ...records.protocol import REFERENCE_URLS, STAGE_TITLES, STAGES, list_proposals, records_dir
    from ...records.report import redact_paths
    from ...records.snapshots import thumbnail_data_uri

    base = Path(cfg.config_path).parent if cfg.config_path else None

    root = records_dir(cfg) / "proposals"
    out = []
    for r in list_proposals(cfg):
        p = r["proposal"]
        for o in p["options"]:
            o.pop("figure_origins", None)
            thumbs = []
            for f in o["figures"]:
                try:
                    thumbs.append(thumbnail_data_uri(root / f, cfg.agent.thumbnail_width_px, cfg.ui.jpeg_quality))
                except OSError as exc:
                    LOG.warning("ui: cannot read figure %s: %s", f, exc)
            o["thumbs"] = thumbs
        prov = p.get("provenance") or {}
        prov.pop("run_dir", None)
        prov.pop("manifest", None)
        for e in p.get("evidence") or []:
            if e.get("path"):
                e["path"] = Path(e["path"]).name
        for key in ("title", "rationale", "provenance", "evidence"):
            if key in p:
                p[key] = redact_paths(p[key], base)
        for o in p["options"]:
            for key in ("label", "description", "table", "numbers"):
                if key in o:
                    o[key] = redact_paths(o[key], base)
        if r.get("decision"):
            r["decision"] = redact_paths(r["decision"], base)
        out.append(r)
    return {"proposals": out, "stages": list(STAGES), "stage_titles": STAGE_TITLES,
            "require_decisions": cfg.agent.require_decisions, "reference_urls": REFERENCE_URLS,
            "table_max_rows": cfg.ui.card_table_max_rows, "table_max_columns": cfg.ui.card_table_max_columns}


def _llm_destination(provider: str, url: str | None) -> str:
    """Human-readable destination of assistant requests (host of the endpoint)."""
    if provider == "mock":
        return "the built-in mock provider (local test mode)"
    if not url:
        return "an unconfigured endpoint"
    from urllib.parse import urlparse

    return urlparse(url).netloc or url
