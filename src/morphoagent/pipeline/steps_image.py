"""Steps 1-5: load, enhance-preview, enhance, segment-tune, segment."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile
from PIL import Image

from .common import (
    LOG,
    MissingInputError,
    RunContext,
    condition_order,
    load_gray,
    prepare_step_dir,
    progress,
    read_inventory,
    require_step,
    sample_images,
    step_dir,
    write_manifest,
)
from .config import Config, shape_grid_factor
from .overlay import contact_sheet, grid_sheet, outline_panel, sheet_svg
from .presets import get_preset, preview_metrics
from .segmentation import CellposeRunner, measure_objects, segment_image


# --------------------------------------------------------------------------- #
# Step 1 - load
# --------------------------------------------------------------------------- #
def parse_name(stem: str, rx: re.Pattern[str], prefix: str, suffix: str) -> tuple[str, str] | None:
    """Return (condition, field) parsed from a file stem, or ``None`` if it does not match."""
    if prefix and stem.startswith(prefix):
        stem = stem[len(prefix):]
    if suffix and stem.endswith(suffix):
        stem = stem[: -len(suffix)]
    m = rx.match(stem)
    if not m:
        return None
    return m.group("condition"), m.group("field")


def run_load(cfg: Config, ctx: RunContext) -> None:
    """List and validate images, parse condition/field, write the inventory."""
    ic = cfg.input
    src = Path(ic.image_dir)
    if not src.is_dir():
        raise MissingInputError(f"input.image_dir does not exist: {src}")
    rx = re.compile(ic.name_regex)
    if not {"condition", "field"} <= set(rx.groupindex):
        raise ValueError("input.name_regex must define the named groups 'condition' and 'field'")
    exts = {e.lower() for e in ic.extensions}
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in exts)
    if not files:
        raise MissingInputError(f"no image with extension {sorted(exts)} in {src}")
    rows, bad = [], []
    for p in files:
        parsed = parse_name(p.stem, rx, ic.strip_prefix, ic.strip_suffix)
        if parsed is None:
            bad.append(p.name)
            continue
        cond, fld = parsed
        rows.append({"image": f"{cond}_{fld}", "condition": cond, "field": fld, "file": p.name, "path": str(p)})
    if bad:
        raise ValueError(f"{len(bad)} file name(s) do not match input.name_regex after stripping prefix/suffix, e.g. {bad[:5]}")
    inv = pd.DataFrame(rows)
    dup = inv[inv.image.duplicated(keep=False)]
    if len(dup):
        raise ValueError(f"duplicate image ids (condition_field): {sorted(set(dup.image))[:5]}")
    d = prepare_step_dir(cfg, 1, ctx.overwrite)
    stats = []
    for r in progress(inv.itertuples(), "load", len(inv)):
        g = load_gray(r.path)
        stats.append({"height": g.shape[0], "width": g.shape[1], "max": int(g.max()), "mean": round(float(g.mean()), 3)})
    inv = pd.concat([inv, pd.DataFrame(stats)], axis=1)
    inv["status"] = np.where(inv["max"] <= ic.skip_max_at_or_below, "all_zero", "ok")
    order = condition_order(cfg, inv.condition)
    inv["_o"] = inv.condition.map(order.index)
    inv = inv.sort_values(["_o", "field"]).drop(columns="_o").reset_index(drop=True)
    inv.to_csv(d / "inventory.csv", index=False)
    skipped = inv[inv.status != "ok"]
    skipped[["image", "file", "max"]].to_csv(d / "skipped_all_zero.csv", index=False)
    per = inv.groupby("condition").agg(n_files=("image", "size"), n_ok=("status", lambda s: int((s == "ok").sum())))
    per["n_skipped"] = per.n_files - per.n_ok
    per = per.reindex(order).reset_index()
    per.to_csv(d / "images_per_condition.csv", index=False)
    for c in per.itertuples():
        LOG.info("  %-24s %3d images (%d skipped)", c.condition, c.n_ok, c.n_skipped)
    if len(skipped):
        LOG.warning("skipped %d all-zero capture(s): %s", len(skipped), ", ".join(skipped.file))
    write_manifest(
        d, cfg, 1, {"input": vars(ic)}, {"files": len(inv)},
        {"images_ok": int((inv.status == "ok").sum()), "images_skipped_all_zero": int(len(skipped)),
         "conditions": len(order), "images_per_condition": dict(zip(per.condition, per.n_ok.astype(int), strict=True))},
        skipped_files=list(skipped.file),
    )
    LOG.info("step 1 done: %d images ok, %d skipped -> %s", int((inv.status == 'ok').sum()), len(skipped), d)


# --------------------------------------------------------------------------- #
# Step 2 - enhance-preview
# --------------------------------------------------------------------------- #
def run_enhance_preview(cfg: Config, ctx: RunContext) -> None:
    """Apply every preview preset to sample images; write a contact sheet and metrics."""
    inv = read_inventory(cfg)
    ec = cfg.enhance
    order = condition_order(cfg, inv.condition)
    samples = sample_images(inv, ec.samples, ec.max_sample_conditions, order)
    d = prepare_step_dir(cfg, 2, ctx.overwrite)
    (d / "images").mkdir()
    presets = list(ec.preview_presets)
    rows, panels, captions, layout = [], [], [], []
    by_id = inv.set_index("image")
    for i, im in enumerate(progress(samples, "enhance-preview")):
        gray = load_gray(by_id.loc[im, "path"])
        prow, crow = [], []
        for j, name in enumerate(presets):
            out, info = get_preset(name, ec)(gray)
            f = d / "images" / f"{im}__{name}.png"
            Image.fromarray(out).save(f, compress_level=3)
            met = preview_metrics(out, ec)
            rows.append({"image": im, "condition": by_id.loc[im, "condition"], "preset": name,
                         "is_default": name == ec.preset, "width": out.shape[1], "height": out.shape[0], **met,
                         "info": json.dumps(info)})
            panel = np.array(Image.fromarray(out).resize(
                (ec.sheet_panel_width_px, int(round(out.shape[0] * ec.sheet_panel_width_px / out.shape[1]))),
                Image.Resampling.LANCZOS))
            prow.append(panel)
            crow.append([im, f"edge {met['edge_strength']:.2f} noise {met['flat_noise']:.2f} p50 {met['p50']:.0f}"])
            layout.append({"row": i, "col": j, "image": im, "preset": name, "file": f"images/{f.name}"})
        panels.append(prow)
        captions.append(crow)
    titles = [f"{p}{' (default)' if p == ec.preset else ''}" for p in presets]
    sheet, items = contact_sheet(panels, captions, titles, cfg.review.font_path)
    sheet.save(d / "contact_sheet.png", compress_level=3, dpi=(300, 300))
    raster, _ = contact_sheet(panels, captions, titles, cfg.review.font_path, draw_text=False)
    (d / "contact_sheet.svg").write_text(sheet_svg(raster, items), encoding="utf-8")
    pd.DataFrame(layout).to_csv(d / "source_contact_sheet.csv", index=False)
    met = pd.DataFrame(rows)
    met.to_csv(d / "preview_metrics.csv", index=False)
    write_manifest(d, cfg, 2, {"enhance": _asdict(ec)}, {"sample_images": samples},
                   {"previews": len(rows), "presets": presets})
    LOG.info("step 2 done: %d samples x %d presets -> %s (contact_sheet.png, preview_metrics.csv)", len(samples), len(presets), d)


def _asdict(obj: object) -> dict:
    import dataclasses

    return dataclasses.asdict(obj)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Step 3 - enhance
# --------------------------------------------------------------------------- #
def run_enhance(cfg: Config, ctx: RunContext) -> None:
    """Apply the chosen preset (``enhance.preset``) to every valid image."""
    inv = read_inventory(cfg)
    ec = cfg.enhance
    fn = get_preset(ec.preset, ec)
    d = prepare_step_dir(cfg, 3, ctx.overwrite)
    (d / "images").mkdir()
    rows = []
    for r in progress(inv.itertuples(), f"enhance [{ec.preset}]", len(inv)):
        out, info = fn(load_gray(r.path))
        Image.fromarray(out).save(d / "images" / f"{r.image}_enhanced.png", compress_level=3)
        rows.append({"image": r.image, "condition": r.condition, "field": r.field, "width": out.shape[1],
                     "height": out.shape[0], **preview_metrics(out, ec), "info": json.dumps(info)})
    pd.DataFrame(rows).to_csv(d / "enhance_log.csv", index=False)
    params = {"preset": ec.preset}
    sub = {"figure-default": ec.figure_default, "tone-unsharp": ec.tone_unsharp, "clahe": ec.clahe}.get(ec.preset)
    if sub is not None:
        params["preset_parameters"] = _asdict(sub)
    write_manifest(d, cfg, 3, params, {"images": len(inv)}, {"enhanced_images": len(rows)})
    LOG.info("step 3 done: %d images enhanced with %s -> %s", len(rows), ec.preset, d / "images")


def enhanced_or_raw(cfg: Config, image: str, raw_path: str, shape: tuple[int, int]) -> np.ndarray:
    """Enhanced image of step 3 resized to ``shape`` if available, otherwise the raw grey image."""
    p = step_dir(cfg, 3) / "images" / f"{image}_enhanced.png"
    if p.is_file():
        a = np.array(Image.open(p).convert("L"))
        if a.shape != tuple(shape):
            a = np.array(Image.fromarray(a).resize((shape[1], shape[0]), Image.Resampling.LANCZOS))
        return a
    return load_gray(raw_path)


# --------------------------------------------------------------------------- #
# Step 4 - segment-tune
# --------------------------------------------------------------------------- #
def tune_settings(cfg: Config) -> pd.DataFrame:
    """Grid of Cellpose settings (cellprob fastest) with the production default flagged."""
    t, s = cfg.segment.tune, cfg.segment
    rows = []
    for ds, dia, flow, cp in itertools.product(t.downscale, t.diameter, t.flow_threshold, t.cellprob_threshold):
        rows.append({"downscale": ds, "diameter": dia, "flow_threshold": flow, "cellprob_threshold": cp})
    g = pd.DataFrame(rows)

    def is_default(r: pd.Series) -> bool:
        dia_eq = (pd.isna(r.diameter) and s.diameter is None) or (s.diameter is not None and r.diameter == s.diameter)
        return bool(dia_eq and r.downscale == s.downscale and r.flow_threshold == s.flow_threshold
                    and r.cellprob_threshold == s.cellprob_threshold)

    g["is_default"] = g.apply(is_default, axis=1)
    if not g.is_default.any():
        g = pd.concat([pd.DataFrame([{"downscale": s.downscale, "diameter": s.diameter, "flow_threshold": s.flow_threshold,
                                      "cellprob_threshold": s.cellprob_threshold, "is_default": True}]), g], ignore_index=True)
    g.insert(0, "setting", [f"S{i:02d}" for i in range(1, len(g) + 1)])
    g["diameter"] = g.diameter.astype(object).where(pd.notna(g.diameter), None)
    return g


def run_segment_tune(cfg: Config, ctx: RunContext) -> None:
    """Run the Cellpose parameter grid on sample images and write outline montages."""
    inv = read_inventory(cfg)
    t = cfg.segment.tune
    order = condition_order(cfg, inv.condition)
    samples = sample_images(inv, t.samples, t.max_sample_conditions, order)
    grid = tune_settings(cfg)
    d = prepare_step_dir(cfg, 4, ctx.overwrite)
    (d / "montages").mkdir()
    grid.to_csv(d / "settings.csv", index=False)
    runner = CellposeRunner(cfg.segment.model, cfg.segment.gpu)
    by_id = inv.set_index("image")
    ncol = len(t.cellprob_threshold) * len(t.flow_threshold)
    rows = []
    jobs = [(im, s) for im in samples for s in grid.itertuples()]
    panels: dict[str, list[tuple[np.ndarray, str, object]]] = {im: [] for im in samples}
    cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for im, s in progress(jobs, "segment-tune", len(jobs)):
        if im not in cache:
            gray = load_gray(by_id.loc[im, "path"])
            cache = {im: (gray, enhanced_or_raw(cfg, im, by_id.loc[im, "path"], gray.shape))}
        gray, bg = cache[im]
        lab, rt = segment_image(runner, gray, s.downscale, s.flow_threshold, s.cellprob_threshold, s.diameter)
        areas = np.bincount(lab.ravel())[1:]
        areas = areas[areas > 0]
        n_big = int((areas >= cfg.select.area_min_px).sum())
        rows.append({"image": im, "setting": s.setting, "downscale": s.downscale, "diameter": s.diameter,
                     "flow_threshold": s.flow_threshold, "cellprob_threshold": s.cellprob_threshold,
                     "is_default": s.is_default, "n_objects": int(len(areas)), "n_objects_area_ge_min": n_big,
                     "runtime_s": round(rt, 2)})
        dia = "auto" if s.diameter is None else f"{s.diameter:g}"
        cap = (f"{s.setting}{' DEFAULT' if s.is_default else ''}  n={len(areas)} (>= area_min {n_big})\n"
               f"ds {s.downscale:g} dia {dia} flow {s.flow_threshold:g} cp {s.cellprob_threshold:g}")
        panels[im].append((outline_panel(bg, lab, t.panel_width_px, t.outline_color), cap, s.setting))
    counts = pd.DataFrame(rows)
    counts.to_csv(d / "tune_counts.csv", index=False)
    for im in samples:
        items = panels[im]
        prow = [items[i : i + ncol] for i in range(0, len(items), ncol)]
        sheet = grid_sheet([[p for p, _, _ in r] for r in prow], [[c for _, c, _ in r] for r in prow], None,
                           cfg.review.font_path)
        Image.fromarray(sheet).save(d / "montages" / f"{im}_tune-montage.png", compress_level=3)
        pd.DataFrame([{"row": i // ncol, "col": i % ncol, "setting": st} for i, (_, _, st) in enumerate(items)]).merge(
            counts[counts.image == im], on="setting").to_csv(d / "montages" / f"source_{im}_tune-montage.csv", index=False)
    write_manifest(d, cfg, 4, {"model": cfg.segment.model, "grid": _asdict(t)}, {"sample_images": samples},
                   {"settings": len(grid), "segmentations": len(rows)}, gpu_used=runner.gpu)
    LOG.info("step 4 done: %d settings x %d samples -> %s", len(grid), len(samples), d / "montages")


# --------------------------------------------------------------------------- #
# Step 5 - segment
# --------------------------------------------------------------------------- #
def run_segment(cfg: Config, ctx: RunContext) -> None:
    """Cellpose-SAM on the downscaled raw grey image; full-size labels and measurements."""
    inv = read_inventory(cfg)
    s = cfg.segment
    d = prepare_step_dir(cfg, 5, ctx.overwrite)
    (d / "labels").mkdir()
    runner = CellposeRunner(s.model, s.gpu)
    objs, imgs = [], []
    for r in progress(inv.itertuples(), "segment", len(inv)):
        gray = load_gray(r.path)
        lab, rt = segment_image(runner, gray, s.downscale, s.flow_threshold, s.cellprob_threshold, s.diameter)
        tifffile.imwrite(d / "labels" / f"{r.image}_labels.tif", lab, compression=s.label_compression or None)
        m = measure_objects(lab, gray, shape_grid_factor(cfg))
        m.insert(0, "field", r.field)
        m.insert(0, "condition", r.condition)
        m.insert(0, "image", r.image)
        objs.append(m)
        imgs.append({"image": r.image, "condition": r.condition, "field": r.field, "n_objects": len(m),
                     "runtime_s": round(rt, 2), "height": gray.shape[0], "width": gray.shape[1]})
    objects = pd.concat(objs, ignore_index=True)
    objects.to_csv(d / "objects.csv", index=False)
    images = pd.DataFrame(imgs)
    images.to_csv(d / "images.csv", index=False)
    params = {k: getattr(s, k) for k in ("model", "flow_threshold", "cellprob_threshold", "diameter", "downscale")}
    params["input"] = "raw grey image (not the enhanced image)"
    params["upscaling"] = "nearest neighbour to the original size"
    params["shape_grid"] = cfg.measure.shape_grid
    write_manifest(d, cfg, 5, params, {"images": len(inv)},
                   {"label_tifs": len(images), "objects": len(objects),
                    "objects_per_condition": objects.groupby("condition").size().to_dict()},
                   gpu_used=runner.gpu)
    LOG.info("step 5 done: %d images, %d objects -> %s", len(images), len(objects), d)


def segment_labels_path(cfg: Config, image: str) -> Path:
    """Label TIFF of step 5 for an image id."""
    return require_step(cfg, 5) / "labels" / f"{image}_labels.tif"
