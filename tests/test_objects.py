"""Segmentation helpers, selection rules and curation edits on synthetic label images."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from morphoagent.pipeline.curation import CurationError, apply_edits, read_edits
from morphoagent.pipeline.segmentation import downscale_gray, frame_contact, measure_objects, upscale_labels
from morphoagent.pipeline.selection import group_rule, image_trim_mask, largest_piece


def rect_labels() -> np.ndarray:
    lab = np.zeros((60, 80), np.int32)
    lab[10:20, 10:40] = 1  # 10 x 30 rectangle
    lab[0:5, 50:60] = 2  # touches the top frame along 10 px
    lab[30:50, 30:50] = 3
    return lab


def test_measure_objects_rectangle() -> None:
    m = measure_objects(rect_labels()).set_index("label")
    assert m.loc[1, "area_px"] == 300
    assert m.loc[1, "aspect_ratio"] == pytest.approx(3.0, rel=0.05)
    assert m.loc[2, "border_contact_px"] == 10
    assert m.loc[1, "border_contact_px"] == 0
    assert 0 < m.loc[3, "form_factor"] <= 1.1


def test_frame_contact_corner_counts_pixels_once() -> None:
    lab = np.zeros((10, 10), np.int32)
    lab[0:3, 0:3] = 1
    assert frame_contact(lab)[1] == 5


def test_down_and_upscale() -> None:
    g = np.arange(64 * 48, dtype=np.uint16).reshape(48, 64) % 256
    small = downscale_gray(g.astype(np.uint8), 0.25)
    assert small.shape == (12, 16) and small.dtype == np.uint8
    lab = np.zeros((12, 16), np.int32)
    lab[2:4, 3:6] = 7
    up = upscale_labels(lab, (48, 64))
    assert up.dtype == np.uint16 and (up == 7).sum() == 2 * 3 * 16


def test_largest_piece_keeps_biggest_component() -> None:
    lab = np.zeros((30, 30), np.int32)
    lab[2:10, 2:10] = 5
    lab[20:22, 20:22] = 5  # small detached piece
    out, removed = largest_piece(lab, 8)
    assert removed == {5: 4}
    assert (out == 5).sum() == 64


def test_group_rule_order() -> None:
    g = group_rule(pd.Series([100, 5000, 5000]), pd.Series([0, 30, 10]), 2000, 24)
    assert list(g) == ["below_area_min", "frame_contact", "kept"]


def test_image_trim_mask_drops_atypical_image() -> None:
    rng = np.random.default_rng(0)
    rows = []
    for i in range(5):
        for _ in range(20):
            big = i == 4
            rows.append({"condition": "A", "image": f"A_{i}", "area_px": rng.normal(20000 if big else 5000, 100),
                         "form_factor": 0.5, "solidity": 0.8, "eccentricity": 0.7, "aspect_ratio": 1.5, "extent": 0.6})
    d = pd.DataFrame(rows)
    keep = image_trim_mask(d, 0.2, ["form_factor", "solidity", "eccentricity", "aspect_ratio", "extent"], True, 2)
    assert set(d.loc[~keep, "image"]) == {"A_4"}
    assert image_trim_mask(d, 0.0, [], True, 2).all()
    keep = image_trim_mask(d, 0.9, ["form_factor"], True, 2)
    assert d.loc[keep, "image"].nunique() == 2  # never fewer than min_images


def _edits(rows: list[dict]) -> list:
    return read_edits(pd.DataFrame(rows), {"X_Image001"})


def test_curation_delete_and_merge_with_gap_fill() -> None:
    lab = np.zeros((20, 20), np.int32)
    lab[2:10, 2:6] = 3
    lab[2:10, 6:10] = 4
    lab[5, 5:7] = 0  # enclosed unlabelled gap between 3 and 4
    lab[15:18, 15:18] = 9
    e = _edits([{"operation": "merge", "image": "X_Image001", "labels": "4;3", "target": ""},
                {"operation": "delete", "image": "X_Image001", "labels": "9", "target": ""}])
    out = apply_edits(lab, e, 8)
    assert e[0].target == 3  # default target = smallest label
    assert set(np.unique(out)) == {0, 3}
    assert (out == 3).sum() == 64  # the two gap pixels are filled
    assert any(x["action"] == "fill_enclosed_gaps" for x in e[0].log)


def test_curation_merge_of_separate_objects_fails() -> None:
    lab = np.zeros((20, 20), np.int32)
    lab[1:4, 1:4] = 1
    lab[10:13, 10:13] = 2
    with pytest.raises(CurationError, match="separate pieces"):
        apply_edits(lab, _edits([{"operation": "merge", "image": "X_Image001", "labels": "1;2"}]), 8)


def test_curation_validation() -> None:
    with pytest.raises(CurationError, match="unknown image"):
        _edits([{"operation": "delete", "image": "Y", "labels": "1"}])
    with pytest.raises(CurationError, match="operation"):
        _edits([{"operation": "split", "image": "X_Image001", "labels": "1"}])
    lab = np.zeros((5, 5), np.int32)
    with pytest.raises(CurationError, match="not present"):
        apply_edits(lab, _edits([{"operation": "delete", "image": "X_Image001", "labels": "7"}]))
