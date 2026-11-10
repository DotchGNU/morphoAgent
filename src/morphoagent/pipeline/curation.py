"""Manual curation edits (step 8): delete and merge labels.

Curation CSV columns
--------------------
operation : ``delete`` or ``merge``
image     : image id ``<condition>_<field>`` as in every table
labels    : label numbers separated by ``;`` (``12`` or ``17;18``)
target    : (merge only, optional) label that survives; default = smallest
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import ndimage as ndi

from .selection import structure


class CurationError(ValueError):
    """Invalid curation CSV row or an edit that cannot be applied."""


@dataclass
class Edit:
    """One parsed curation row."""

    row: int
    operation: str
    image: str
    labels: list[int]
    target: int | None = None
    log: list[dict] = field(default_factory=list)


def _parse_labels(text: object, row: int) -> list[int]:
    parts = [p for p in str(text).replace(",", ";").replace(" ", ";").split(";") if p.strip()]
    try:
        out = [int(float(p)) for p in parts]
    except ValueError as exc:
        raise CurationError(f"row {row}: cannot parse labels {text!r}") from exc
    if not out:
        raise CurationError(f"row {row}: no labels given")
    return out


def read_edits(df: pd.DataFrame, known_images: set[str]) -> list[Edit]:
    """Validate and parse a curation table (see module docstring)."""
    need = {"operation", "image", "labels"}
    missing = need - set(df.columns)
    if missing:
        raise CurationError(f"curation CSV is missing column(s) {sorted(missing)}")
    edits = []
    for i, r in df.iterrows():
        row = int(i) + 2  # 1-based line number including the header
        op = str(r["operation"]).strip().lower()
        img = str(r["image"]).strip()
        if op not in ("delete", "merge"):
            raise CurationError(f"row {row}: operation must be 'delete' or 'merge', got {r['operation']!r}")
        if img not in known_images:
            raise CurationError(f"row {row}: unknown image {img!r}")
        labels = _parse_labels(r["labels"], row)
        target = None
        if op == "merge":
            t = r.get("target") if "target" in df.columns else None
            target = int(float(t)) if t is not None and pd.notna(t) and str(t).strip() != "" else min(labels)
            if target not in labels:
                labels = [target, *labels]
            if len(set(labels)) < 2:
                raise CurationError(f"row {row}: merge needs at least two different labels")
        edits.append(Edit(row, op, img, labels, target))
    return edits


def apply_edits(labels: np.ndarray, edits: list[Edit], connectivity: int = 8) -> np.ndarray:
    """Apply delete / merge edits of one image to a copy of its label image.

    Merge: every other label is relabelled to ``target``; unlabelled pixels
    enclosed by the merged object are filled (``binary_fill_holes`` restricted
    to background); the merged object must form one connected component.
    Each edit's log entries are appended to ``edit.log``.
    """
    cur = labels.astype(np.int32).copy()
    st = structure(connectivity)
    for e in edits:
        present = set(np.unique(cur).tolist())
        absent = [lb for lb in e.labels if lb not in present]
        if absent:
            raise CurationError(f"row {e.row} ({e.image}): label(s) {absent} not present (already deleted or merged?)")
        if e.operation == "delete":
            for lb in e.labels:
                n = int((cur == lb).sum())
                cur[cur == lb] = 0
                e.log.append({"action": "delete", "label": lb, "pixels": -n, "note": ""})
            continue
        a = e.target
        for b in e.labels:
            if b == a:
                continue
            gap = float(ndi.distance_transform_edt(cur != a)[cur == b].min())
            n = int((cur == b).sum())
            cur[cur == b] = a
            e.log.append({"action": "merge", "label": b, "pixels": n, "note": f"into {a}; gap {gap:.1f} px"})
        holes = ndi.binary_fill_holes(cur == a) & (cur == 0)
        if holes.any():
            cur[holes] = a
            e.log.append({"action": "fill_enclosed_gaps", "label": a, "pixels": int(holes.sum()), "note": ""})
        _, ncomp = ndi.label(cur == a, structure=st)
        if ncomp != 1:
            raise CurationError(
                f"row {e.row} ({e.image}): merged object {a} has {ncomp} separate pieces "
                f"({connectivity}-connectivity); merge only touching objects"
            )
    return cur
