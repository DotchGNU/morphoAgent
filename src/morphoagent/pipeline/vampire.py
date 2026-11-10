"""Step 9: VAMPIRE shape modes (main-environment side).

Prepares one image set per condition (label TIFFs that contain only the kept
cells), runs :mod:`morphoagent.pipeline.vampire_runner` with the interpreter given in
``vampire.python``, then post-processes the results: elbow suggestions,
renumbering of modes by ascending mean aspect ratio, per-cell modes, per-mode
morphometry and mean-contour figures.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile
from PIL import Image

from .common import LOG, RunContext, analysis_set, condition_order, label_path, prepare_step_dir, write_manifest
from .config import Config
from .plotting import apply_style, elbow_plot, mode_contours, save_figure

SHAPE5 = ["form_factor", "solidity", "eccentricity", "aspect_ratio", "extent"]


def elbow_suggestions(k: np.ndarray, inertia: np.ndarray) -> tuple[int, int, np.ndarray, np.ndarray]:
    """Second-difference and kneedle K suggestions.

    second difference at K = I(K-1) - 2 I(K) + I(K+1) (maximum over interior K);
    kneedle = argmax of (1 - x_n) - y_n with K and inertia min-max normalised.

    Returns
    -------
    (k_second_diff, k_kneedle, second_diff, kneedle_distance)
    """
    sd = np.full(len(inertia), np.nan)
    sd[1:-1] = inertia[:-2] - 2 * inertia[1:-1] + inertia[2:]
    xn = (k - k.min()) / (k.max() - k.min())
    yn = (inertia - inertia.min()) / (inertia.max() - inertia.min())
    kd = (1 - xn) - yn
    return int(k[1:-1][np.nanargmax(sd[1:-1])]), int(k[np.argmax(kd)]), sd, kd


def display_mapping(cells: pd.DataFrame, k: int, renumber_by: str) -> pd.DataFrame:
    """Raw VAMPIRE mode -> display mode (1..K by ascending mean aspect ratio)."""
    g = cells.groupby("raw_shape_mode").agg(n_objects=("label", "size"), mean_aspect_ratio=("aspect_ratio", "mean"))
    g = g.reindex(range(1, k + 1))
    if renumber_by == "aspect_ratio":
        g["display_mode"] = g.mean_aspect_ratio.rank(method="first", na_option="bottom").astype(int)
    else:
        g["display_mode"] = g.index
    return g.reset_index()


def _prepare_sets(cfg: Config, cells: pd.DataFrame, fields: pd.DataFrame, work: Path) -> pd.DataFrame:
    """Write one folder per condition with masked uint16 label TIFFs (tag 'labels')."""
    fmap = fields.set_index("image").label_tif.to_dict()
    sets, files = [], []
    order = condition_order(cfg, cells.condition)
    for i, c in enumerate(order, 1):
        sdir = work / "sets" / f"set{i:02d}"
        sdir.mkdir(parents=True)
        sub = cells[cells.condition == c]
        imgs = sorted(sub.image.unique())
        n_per = sub.groupby("image").size()
        # VAMPIRE treats a whole set as an unlabelled binary mask when its FIRST image (by file
        # name) contains a single object; put an image with >= 2 objects first to avoid that.
        multi = [im for im in imgs if n_per[im] >= 2]
        if not multi:
            raise RuntimeError(f"condition {c!r}: every image has < 2 kept objects; VAMPIRE would mis-read the set")
        imgs = [multi[0]] + [im for im in imgs if im != multi[0]]
        for j, im in enumerate(imgs):
            lab = tifffile.imread(label_path(cfg, fmap[im])).astype(np.int32)
            keep = sub.loc[sub.image == im, "label"].to_numpy()
            if not np.isin(keep, np.unique(lab)).all():
                raise RuntimeError(f"{im}: kept label(s) missing from {fmap[im]}")
            lab[~np.isin(lab, keep)] = 0
            name = f"{j:04d}_{im}_labels.tif"
            Image.fromarray(lab.astype(np.uint16)).save(sdir / name)
            files.append({"filename": name, "condition": c, "image": im, "set_location": str(sdir)})
        sets.append({"set ID": i, "condition": c, "set location": str(sdir), "tag": "labels", "note": "morphoagent"})
    pd.DataFrame(sets).to_csv(work / "image_sets.csv", index=False)
    pd.DataFrame(files).to_csv(work / "file_map.csv", index=False)
    return pd.DataFrame(sets)


def run_vampire(cfg: Config, ctx: RunContext) -> None:
    """Pool all conditions into one VAMPIRE model and assign shape modes to every kept cell."""
    vc = cfg.vampire
    src = Path(vc.src_path)
    for f in ("getboundary.py", "mainbody.py", "clusterSM.py", "pca_bdreg.py"):
        if not (src / f).is_file():
            raise FileNotFoundError(f"vampire.src_path does not look like VAMPIRE_open 'Source Code': {src / f} missing")
    _, cells, fields, from_step = analysis_set(cfg)
    d = prepare_step_dir(cfg, 9, ctx.overwrite)
    work = d / "vampire_work"
    work.mkdir()
    sets = _prepare_sets(cfg, cells, fields, work)
    job = {"work_dir": str(work), "vampire_src": str(src), "n_coordinates": vc.n_coordinates, "k": [int(k) for k in vc.k],
           "model_name": "morphoagent", "elbow_k_min": vc.elbow.k_min, "elbow_k_max": vc.elbow.k_max,
           "elbow_kmeans": dataclasses.asdict(vc.elbow_kmeans)}
    (work / "job.json").write_text(json.dumps(job, indent=2))
    runner = Path(__file__).with_name("vampire_runner.py")
    env = dict(os.environ, PYTHONNOUSERSITE="1", MPLBACKEND="Agg")
    env.pop("PYTHONPATH", None)
    cmd = [vc.python, str(runner), str(work / "job.json")]
    LOG.info("running VAMPIRE: %s (log: %s)", " ".join(cmd), work / "runner.log")
    with open(work / "runner.log", "w") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                cwd=str(work))
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if line.startswith("[runner]"):
                (LOG.debug if line.startswith("[runner] elbow") else LOG.info)(line.rstrip())
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"VAMPIRE runner failed (exit {rc}); see {work / 'runner.log'}")
    meta = json.loads((work / "runner_meta.json").read_text())
    _analyse(cfg, d, work, cells, meta, from_step, len(sets))


def _analyse(cfg: Config, d: Path, work: Path, cells: pd.DataFrame, meta: dict, from_step: int, n_sets: int) -> None:
    vc = cfg.vampire
    apply_style(cfg.plot)
    fig_dir = d / "figures"
    el = pd.read_csv(work / "elbow_raw.csv")
    k_sd, k_kn, sd, kd = elbow_suggestions(el.K.to_numpy(), el.inertia.to_numpy())
    el["second_diff"], el["kneedle_distance"] = sd, kd
    el.to_csv(d / "elbow_inertia.csv", index=False)
    monotonic = bool((np.diff(el.inertia) < 0).all())
    agree = {}
    for k, mk in meta["per_k"].items():
        row = el[int(k) == el.K]
        if len(row):
            agree[k] = bool(abs(float(row.inertia.iloc[0]) - mk["kmeans_inertia_recorded"]) < 1e-6)
    (d / "elbow_suggestions.json").write_text(json.dumps(
        {"k_second_diff": k_sd, "k_kneedle": k_kn, "inertia_monotonic_decreasing": monotonic,
         "elbow_inertia_equals_vampire_fit": agree}, indent=2))
    fig, srcdf = elbow_plot(el, k_sd, k_kn, [int(k) for k in vc.k], cfg.stats.box.line_width_pt, len(cells))
    save_figure(fig, fig_dir, "elbow_inertia", srcdf, cfg.plot.dpi)
    out_counts: dict[str, object] = {}
    metric_cols = [c for c in SHAPE5 + ["area_px"] if c in cells]
    for k in [int(x) for x in vc.k]:
        raw = pd.read_csv(work / "runs" / f"k{k}" / "cells_vampire_raw.csv")
        m = raw.merge(cells[["image", "label", *metric_cols]], on=["image", "label"], how="left", validate="1:1")
        m["area_abs_diff_px"] = (m.vampire_area_px - m.area_px).abs()
        if m.area_px.isna().any():
            raise RuntimeError("VAMPIRE returned objects that are not in the cells table")
        mapping = display_mapping(m, k, vc.renumber_by)
        mapping.to_csv(d / f"mode_mapping_K{k}.csv", index=False)
        m["shape_mode"] = m.raw_shape_mode.map(dict(zip(mapping.raw_shape_mode, mapping.display_mode, strict=True)))
        m = m.sort_values(["condition", "image", "label"])
        m[["condition", "image", "label", "shape_mode", "raw_shape_mode", "dist_from_center", *metric_cols,
           "vampire_area_px", "area_abs_diff_px"]].to_csv(d / f"shape_modes_K{k}.csv", index=False)
        agg = {"n_objects": ("label", "size")}
        agg.update({f"{c}_mean": (c, "mean") for c in metric_cols})
        agg.update({"area_px_median": ("area_px", "median")})
        mor = m.groupby("shape_mode").agg(**agg).reset_index().rename(columns={"shape_mode": "mode"})
        mor.insert(1, "raw_shape_mode", mor["mode"].map(dict(zip(mapping.display_mode, mapping.raw_shape_mode, strict=True))))
        mor.to_csv(d / f"mode_morphometry_K{k}.csv", index=False)
        cont = pd.read_csv(work / "runs" / f"k{k}" / "mode_mean_contour_raw.csv")
        cont["display_mode"] = cont.raw_shape_mode.map(dict(zip(mapping.raw_shape_mode, mapping.display_mode, strict=True)))
        n_by = dict(zip(mor["mode"], mor.n_objects, strict=True))
        fig, srcdf = mode_contours(cont, vc.mode_colors, vc.contour_edge_color, n_by, 1.0)
        save_figure(fig, fig_dir, f"mode_mean_contours_K{k}", srcdf, cfg.plot.dpi)
        dropped = cells.merge(m[["image", "label"]], on=["image", "label"], how="left", indicator=True)
        dropped = dropped[dropped._merge == "left_only"].drop(columns="_merge")
        dropped.to_csv(d / f"vampire_dropped_objects_K{k}.csv", index=False)
        out_counts[f"K{k}"] = {"objects_with_mode": len(m), "objects_dropped_by_vampire": len(dropped),
                               "raw_to_display": {
                                   int(a): int(b) for a, b in zip(mapping.raw_shape_mode, mapping.display_mode, strict=True)
                               },
                               "objects_per_display_mode": {int(a): int(b) for a, b in n_by.items()},
                               "max_area_abs_diff_px": float(m.area_abs_diff_px.max())}
        LOG.info("K=%d: raw->display mode %s ; objects per mode %s ; dropped %d", k,
                 out_counts[f"K{k}"]["raw_to_display"], out_counts[f"K{k}"]["objects_per_display_mode"], len(dropped))
    params = dataclasses.asdict(vc)
    params["pca_components"] = meta["pcnum_used"]
    params["input_tables_from_step"] = from_step
    write_manifest(d, cfg, 9, params, {"cells": len(cells), "conditions": n_sets},
                   {"per_k": out_counts, "elbow_k_second_diff": k_sd, "elbow_k_kneedle": k_kn},
                   vampire_runner=meta)
    LOG.info("step 9 done: elbow suggests K=%d (2nd diff) / K=%d (kneedle) -> %s", k_sd, k_kn, d)
