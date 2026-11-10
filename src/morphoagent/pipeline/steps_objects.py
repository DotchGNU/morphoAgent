"""Steps 6-8: select, review, curate."""

from __future__ import annotations

import dataclasses
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile
from PIL import Image

from .common import (
    LOG,
    MANIFEST,
    RunContext,
    condition_order,
    file_sha256,
    label_path,
    load_gray,
    prepare_step_dir,
    progress,
    read_inventory,
    require_step,
    step_dir,
    write_manifest,
)
from .config import Config
from .curation import CurationError, apply_edits, read_edits
from .field_table import (
    SOURCE_FILE,
    SOURCE_SEGMENT,
    FieldTableError,
    check_label_files,
    read_field_table,
    read_label_file,
)
from .overlay import numbered_overlay
from .segmentation import GRID_FULL, GRID_SEGMENTATION, SHAPE_COLUMNS, measure_objects
from .selection import (
    GROUP_FRAME,
    GROUP_KEPT,
    GROUP_SMALL,
    GROUP_TRIM,
    group_rule,
    image_trim_mask,
    largest_piece,
)
from .steps_image import enhanced_or_raw

GROUPS = (GROUP_KEPT, GROUP_FRAME, GROUP_SMALL, GROUP_TRIM)


def _group_counts(df: pd.DataFrame) -> dict[str, int]:
    return {g: int((df.group == g).sum()) for g in GROUPS}


def _fields_table(objects: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    cnt = objects.groupby(["image", "group"]).size().unstack(fill_value=0)
    for g in GROUPS:
        if g not in cnt:
            cnt[g] = 0
    f = base.set_index("image")
    f["n_objects"] = objects.groupby("image").size().reindex(f.index, fill_value=0)
    f["n_kept"] = cnt[GROUP_KEPT].reindex(f.index, fill_value=0)
    f["n_frame_contact"] = cnt[GROUP_FRAME].reindex(f.index, fill_value=0)
    f["n_below_area_min"] = cnt[GROUP_SMALL].reindex(f.index, fill_value=0)
    return f.reset_index()


def _manifest_param(step: Path, key: str, default: str) -> str:
    """A parameter recorded in a step's run manifest (``default`` for older runs without it)."""
    import json

    try:
        return str(json.loads((step / MANIFEST).read_text(encoding="utf-8")).get("parameters", {}).get(key, default))
    except (OSError, ValueError):
        return default


def _changed_labels(lab: np.ndarray, seg: np.ndarray) -> set[int]:
    """Labels of ``lab`` whose mask is not identical to the same label in the segmentation output ``seg``."""
    diff = lab != seg
    cand = set(np.unique(lab[diff]).tolist()) | set(np.unique(seg[diff]).tolist())  # pixels gained or lost
    cand.discard(0)
    return {int(v) for v in cand}


# --------------------------------------------------------------------------- #
# Step 6 - select
# --------------------------------------------------------------------------- #
def run_select(cfg: Config, ctx: RunContext) -> None:
    """Largest piece per label -> image trim (optional) -> area_min -> frame contact."""
    sc = cfg.select
    inv_df = read_inventory(cfg)
    inv = inv_df.set_index("image")
    ftab = read_field_table(sc.field_table, inv_df) if sc.field_table else None
    seg = None
    if ftab is None or (ftab.label_source == SOURCE_SEGMENT).any():
        seg = require_step(cfg, 5)
        obj5 = pd.read_csv(seg / "objects.csv")
        images5 = pd.read_csv(seg / "images.csv")
    seg_grid = GRID_FULL
    if seg is not None:
        seg_grid = _manifest_param(seg, "shape_grid", GRID_FULL)
    if ftab is None:
        images = images5[["image", "condition", "field"]].assign(label_file="", label_source=SOURCE_SEGMENT,
                                                                 shape_grid="", segmentation_label_file="")
    else:
        if seg is not None:
            not_seg = sorted(set(ftab.image[ftab.label_source == SOURCE_SEGMENT]) - set(images5.image))
            if not_seg:
                raise FieldTableError(f"field table: no label_file given and not segmented in step 5: {not_seg[:5]}")
        check_label_files(ftab, inv_df)
        images = ftab
        LOG.info("field table %s: %d field(s), %d with a label file, %d from step 5", sc.field_table, len(ftab),
                 int((ftab.label_source == SOURCE_FILE).sum()), int((ftab.label_source == SOURCE_SEGMENT).sum()))
    d = prepare_step_dir(cfg, 6, ctx.overwrite)
    (d / "labels").mkdir()
    out_rows, frows = [], []
    n_frag_total, px_total = 0, 0
    for r in progress(images.itertuples(), "select", len(images)):
        mode = r.shape_grid or cfg.measure.shape_grid
        factor = cfg.segment.downscale if mode == GRID_SEGMENTATION else None
        shape = (int(inv.loc[r.image, "height"]), int(inv.loc[r.image, "width"]))
        if r.label_source == SOURCE_FILE:
            lab = read_label_file(r.label_file, shape)
            o = None
        else:
            assert seg is not None
            lab = tifffile.imread(seg / "labels" / f"{r.image}_labels.tif")
            o = obj5[obj5.image == r.image].copy() if mode == seg_grid else None
        removed: dict[int, int] = {}
        if sc.largest_piece:
            lab, removed = largest_piece(lab, sc.connectivity)
        changed: set[int] = set(removed)
        if factor is not None and r.segmentation_label_file:
            changed |= _changed_labels(lab, read_label_file(r.segmentation_label_file, shape))
        if removed or o is None:
            m = measure_objects(lab, load_gray(inv.loc[r.image, "path"]), factor, changed)
            m.insert(0, "field", r.field)
            m.insert(0, "condition", r.condition)
            m.insert(0, "image", r.image)
            o = m
        o["fragment_px_removed"] = o.label.map(removed).fillna(0).astype(int)
        rel = Path("06_select") / "labels" / f"{r.image}_labels.tif"
        if lab.max() > np.iinfo(np.uint16).max:
            raise FieldTableError(f"{r.image}: more than 65535 labels; uint16 label TIFFs cannot hold them")
        tifffile.imwrite(Path(cfg.output_dir) / rel, lab.astype(np.uint16, copy=False),
                         compression=cfg.segment.label_compression or None)
        out_rows.append(o)
        frows.append({"image": r.image, "condition": r.condition, "field": r.field, "label_tif": str(rel),
                      "label_source": r.label_source, "label_input": r.label_file, "shape_grid": mode,
                      "n_fragmented_labels": len(removed), "fragment_px_removed": int(sum(removed.values()))})
        n_frag_total += len(removed)
        px_total += int(sum(removed.values()))
    objects = pd.concat(out_rows, ignore_index=True)
    n_in = len(objects)
    objects["group_rule"] = group_rule(objects.area_px, objects.border_contact_px, sc.area_min_px, sc.frame_contact_px)
    trim_keep = image_trim_mask(objects, sc.image_trim_fraction, sc.image_trim_features, sc.image_trim_log_area,
                                sc.image_trim_min_images)
    objects["group"] = np.where(trim_keep, objects.group_rule, GROUP_TRIM)
    fields = pd.DataFrame(frows)
    trimmed_images = sorted(set(objects.loc[~trim_keep, "image"]))
    fields["image_trimmed"] = fields.image.isin(trimmed_images)
    fields = _fields_table(objects, fields)
    cells = objects[objects.group == GROUP_KEPT].copy()
    objects.to_csv(d / "objects.csv", index=False)
    cells.to_csv(d / "cells.csv", index=False)
    fields.to_csv(d / "fields.csv", index=False)
    after_trim = objects[trim_keep]
    tree = {
        "objects_segmented": n_in,
        "largest_piece": {"enabled": sc.largest_piece, "labels_fragmented": n_frag_total, "pixels_removed": px_total},
        "image_trim": {"fraction": sc.image_trim_fraction, "images_in": int(objects.image.nunique()),
                       "images_dropped": len(trimmed_images), "objects_dropped": int((~trim_keep).sum()),
                       "dropped_images": trimmed_images},
        "area_min": {"objects_in": len(after_trim), "dropped_below_area_min": int((after_trim.group == GROUP_SMALL).sum())},
        "frame_contact": {"objects_in": int((after_trim.group != GROUP_SMALL).sum()),
                          "dropped_frame_contact": int((after_trim.group == GROUP_FRAME).sum())},
        "kept": len(cells),
    }
    order = condition_order(cfg, objects.condition)
    per = {c: _group_counts(objects[objects.condition == c]) for c in order}
    ft_info = None
    if ftab is not None:
        ft_info = {"path": sc.field_table, "sha256": file_sha256(sc.field_table), "fields": len(ftab),
                   "fields_with_label_file": int((ftab.label_source == SOURCE_FILE).sum()),
                   "fields_from_step5": int((ftab.label_source == SOURCE_SEGMENT).sum()),
                   "valid_images_not_listed": int(len(inv_df) - len(ftab))}
    grid_counts = objects["shape_grid"].value_counts().to_dict() if "shape_grid" in objects else {}
    params = dataclasses.asdict(sc) | {"shape_grid": cfg.measure.shape_grid, "shape_grid_factor": cfg.segment.downscale}
    write_manifest(d, cfg, 6, params, {"objects": n_in, "images": len(images)},
                   {"kept": len(cells), **_group_counts(objects)}, selection_tree=tree, per_condition=per,
                   field_table=ft_info,
                   shape_grid={"objects_per_grid": {str(k): int(v) for k, v in grid_counts.items()},
                               "kept_per_grid": {str(k): int(v) for k, v in
                                                 cells["shape_grid"].value_counts().to_dict().items()}
                               if "shape_grid" in cells else {}})
    LOG.info("step 6 done: %d objects -> kept %d | frame_contact %d | below_area_min %d | image_trimmed %d",
             n_in, len(cells), *[_group_counts(objects)[g] for g in GROUPS[1:]])


# --------------------------------------------------------------------------- #
# Step 7 - review
# --------------------------------------------------------------------------- #
def _jpeg_bytes(img: np.ndarray, width: int, quality: int) -> io.BytesIO:
    h, w = img.shape[:2]
    pil = Image.fromarray(img)
    if w > width:
        pil = pil.resize((width, int(round(h * width / w))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.convert("RGB").save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return buf


def run_review(cfg: Config, ctx: RunContext) -> None:
    """Numbered overlays for every field and a 16:9 review deck (enhanced | overlay)."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    rc = cfg.review
    sel = require_step(cfg, 6)
    inv = read_inventory(cfg).set_index("image")
    objects = pd.read_csv(sel / "objects.csv")
    fields = pd.read_csv(sel / "fields.csv")
    order = condition_order(cfg, fields.condition)
    fields["_o"] = fields.condition.map(order.index)
    fields = fields.sort_values(["_o", "field"]).drop(columns="_o")
    d = prepare_step_dir(cfg, 7, ctx.overwrite)
    (d / "overlays").mkdir()
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    pic_w, top = Inches(6.4), Inches(0.85)
    n_drawn = 0
    for f in progress(fields.itertuples(), "review", len(fields)):
        lab = tifffile.imread(label_path(cfg, f.label_tif)).astype(np.int32)
        raw_path = inv.loc[f.image, "path"]
        bg = enhanced_or_raw(cfg, f.image, raw_path, lab.shape) if rc.background == "enhanced" else load_gray(raw_path)
        o = objects[objects.image == f.image]
        kept = o.loc[o.group_rule == GROUP_KEPT, "label"].tolist()
        frame = o.loc[o.group_rule == GROUP_FRAME, "label"].tolist()
        ov = numbered_overlay(bg, lab, kept, frame, dict(zip(o.label, o.area_px, strict=True)), rc)
        Image.fromarray(ov).save(d / "overlays" / f"{f.image}_numbered.png", compress_level=3)
        o.sort_values("label")[["label", "group", "group_rule", "area_px", "border_contact_px", "aspect_ratio",
                                "form_factor", "solidity"]].to_csv(d / "overlays" / f"source_{f.image}_numbered.csv",
                                                                   index=False)
        n_drawn += len(kept) + len(frame)
        s = prs.slides.add_slide(blank)
        title = (f"{f.condition}  |  {f.image}  |  kept {len(kept)}, frame contact {len(frame)}, "
                 f"below area_min {int(f.n_below_area_min)}")
        if f.image_trimmed:
            title += "  |  IMAGE TRIMMED (excluded from analysis)"
        tb = s.shapes.add_textbox(Inches(0.2), Inches(0.15), Inches(12.9), Inches(0.5))
        tb.text_frame.text = title
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(16)
        s.shapes.add_picture(_jpeg_bytes(np.stack([bg] * 3, -1), rc.slide_image_width_px, rc.jpeg_quality),
                             Inches(0.2), top, width=pic_w)
        s.shapes.add_picture(_jpeg_bytes(ov, rc.slide_image_width_px, rc.jpeg_quality), Inches(6.73), top, width=pic_w)
        foot = s.shapes.add_textbox(Inches(0.2), Inches(6.9), Inches(12.9), Inches(0.5))
        foot.text_frame.word_wrap = True
        foot.text_frame.text = (
            f"Left: {rc.background} image. Right: red solid = kept (area >= {cfg.select.area_min_px:g} px, frame contact "
            f"<= {cfg.select.frame_contact_px:g} px); pale-red dashed = frame contact; objects below area_min not drawn. "
            "Numbers = label ids used in the curation CSV (image id as in the title).")
        foot.text_frame.paragraphs[0].runs[0].font.size = Pt(10)
    deck = d / "review_deck.pptx"
    prs.save(str(deck))
    pd.DataFrame(columns=["operation", "image", "labels", "target"]).to_csv(d / "curation_template.csv", index=False)
    write_manifest(d, cfg, 7, dataclasses.asdict(rc), {"fields": len(fields)},
                   {"slides": len(fields), "objects_drawn": n_drawn}, deck=str(deck.name))
    LOG.info("step 7 done: %d slides -> %s ; fill %s and set curate.edits_csv", len(fields), deck,
             d / "curation_template.csv")


# --------------------------------------------------------------------------- #
# Step 8 - curate
# --------------------------------------------------------------------------- #
def _ask(question: str) -> bool:
    try:
        ans = input(question)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def run_curate(cfg: Config, ctx: RunContext) -> None:
    """Optional delete / merge edits; re-measures edited objects at full resolution."""
    if ctx.curate_answer == "skip":
        LOG.info("step 8 skipped (--skip); downstream steps use the step-6 selection")
        return
    csv = cfg.curate.edits_csv
    if not csv or not Path(csv).is_file():
        LOG.info("step 8 skipped: no curation CSV (curate.edits_csv = %s); downstream steps use the step-6 selection", csv)
        return
    if ctx.curate_answer != "yes":
        if not sys.stdin.isatty():
            LOG.info("step 8 skipped: not interactive and neither --yes nor --skip given")
            return
        if not _ask("Is curation needed? [y/N] "):
            LOG.info("step 8 skipped by user; downstream steps use the step-6 selection")
            return
    sc = cfg.select
    sel = require_step(cfg, 6)
    objects = pd.read_csv(sel / "objects.csv")
    fields = pd.read_csv(sel / "fields.csv")
    inv = read_inventory(cfg).set_index("image")
    edits = read_edits(pd.read_csv(csv, dtype=str, keep_default_na=False), set(fields.image))
    d = prepare_step_dir(cfg, 8, ctx.overwrite)
    (d / "labels").mkdir()
    fields = fields.set_index("image")
    fields["curated"] = False
    log_rows, new_parts = [], []
    edited_images = list(dict.fromkeys(e.image for e in edits))
    for im in edited_images:
        lab0 = tifffile.imread(label_path(cfg, fields.loc[im, "label_tif"])).astype(np.int32)
        mine = [e for e in edits if e.image == im]
        try:
            cur = apply_edits(lab0, mine, cfg.curate.connectivity)
        except CurationError:
            import shutil

            shutil.rmtree(d)
            raise
        for e in mine:
            for entry in e.log:
                log_rows.append({"csv_row": e.row, "image": im, "operation": e.operation,
                                 "labels": ";".join(map(str, e.labels)), "target": e.target, **entry})
        rel = Path("08_curate") / "labels" / f"{im}_labels_curated.tif"
        tifffile.imwrite(Path(cfg.output_dir) / rel, cur.astype(np.uint16), compression=cfg.segment.label_compression or None)
        fields.loc[im, ["label_tif", "curated"]] = [str(rel), True]
        old = objects[objects.image == im].set_index("label")
        if "shape_grid" not in old:  # selections written before the shape_grid column existed
            old["shape_grid"] = GRID_FULL
        m = measure_objects(cur, load_gray(inv.loc[im, "path"])).set_index("label")
        touched = [lb for lb in m.index if not (lb in old.index and np.array_equal(cur == lb, lab0 == lb))]
        same = [lb for lb in m.index if lb not in touched]
        new = m.copy()
        new.loc[same, SHAPE_COLUMNS] = old.loc[same, SHAPE_COLUMNS].to_numpy()
        new["fragment_px_removed"] = old["fragment_px_removed"].reindex(new.index).fillna(0).astype(int)
        new = new.reset_index()
        new.insert(0, "field", fields.loc[im, "field"])
        new.insert(0, "condition", fields.loc[im, "condition"])
        new.insert(0, "image", im)
        new["touched"] = new.label.isin(touched)
        new["group_rule"] = group_rule(new.area_px, new.border_contact_px, sc.area_min_px, sc.frame_contact_px)
        new["group"] = GROUP_TRIM if bool(fields.loc[im, "image_trimmed"]) else new.group_rule
        new_parts.append((im, new))
        LOG.info("  %s: %d edit row(s), objects %d -> %d, kept %d -> %d, re-measured %s", im, len(mine), len(old),
                 len(new), int((old.group == GROUP_KEPT).sum()), int((new.group == GROUP_KEPT).sum()), touched)
    objects["touched"] = False
    keep = objects[~objects.image.isin(edited_images)]
    cols = list(objects.columns)
    out = pd.concat([keep] + [n[cols] for _, n in new_parts], ignore_index=True)
    order = condition_order(cfg, out.condition)
    out["_o"] = out.condition.map(order.index)
    out = out.sort_values(["_o", "image", "label"]).drop(columns="_o").reset_index(drop=True)
    fields = _fields_table(out, fields.reset_index().drop(columns=["n_objects", "n_kept", "n_frame_contact", "n_below_area_min"]))
    cells = out[out.group == GROUP_KEPT].copy()
    out.to_csv(d / "objects.csv", index=False)
    cells.to_csv(d / "cells.csv", index=False)
    fields.to_csv(d / "fields.csv", index=False)
    pd.DataFrame(log_rows).to_csv(d / "applied_edits.csv", index=False)
    before = pd.read_csv(sel / "cells.csv")
    write_manifest(d, cfg, 8, {"edits_csv": csv, "connectivity": cfg.curate.connectivity,
                               "area_min_px": sc.area_min_px, "frame_contact_px": sc.frame_contact_px},
                   {"edit_rows": len(edits), "images_edited": len(edited_images), "kept_before": len(before)},
                   {"kept": len(cells), "objects": len(out)}, status="applied", edits_csv_sha256=_sha(csv))
    LOG.info("step 8 done: %d edit rows on %d image(s); kept %d -> %d -> %s", len(edits), len(edited_images),
             len(before), len(cells), d)


def _sha(p: str) -> str | None:
    from .common import file_sha256

    return file_sha256(p)


def curate_applied(cfg: Config) -> bool:
    """True when step 8 exists and was applied."""
    p = step_dir(cfg, 8) / MANIFEST
    if not p.is_file():
        return False
    import json

    return json.loads(p.read_text()).get("status") == "applied"
