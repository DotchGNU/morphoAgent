"""End-to-end run of steps 1-8 and 10 on synthetic images (Cellpose replaced by a threshold)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from morphoagent.pipeline.cli import build_parser, main
from morphoagent.pipeline.stats import bh
from morphoagent.pipeline.vampire import display_mapping, elbow_suggestions

from .conftest import REPO


def _manifest(out: Path, step: str) -> dict:
    return json.loads((out / step / "run_manifest.json").read_text())


@pytest.mark.usefixtures("fake_cellpose")
def test_steps_1_to_10_without_vampire(config_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "7"]) == 0
    inv = pd.read_csv(out / "01_load" / "inventory.csv")
    assert set(inv.condition) == {"CTRL", "DRUG_A"}  # condition with an underscore is parsed
    assert (inv.status == "all_zero").sum() == 1
    assert _manifest(out, "01_load")["counts_out"]["images_ok"] == 6
    assert (out / "02_enhance-preview" / "contact_sheet.png").is_file()
    assert len(list((out / "03_enhance" / "images").glob("*_enhanced.png"))) == 6
    assert len(list((out / "04_segment-tune" / "montages").glob("*_tune-montage.png"))) == 2
    assert len(list((out / "05_segment" / "labels").glob("*.tif"))) == 6
    objects = pd.read_csv(out / "06_select" / "objects.csv")
    assert {"kept", "frame_contact"} <= set(objects.group)
    tree = _manifest(out, "06_select")["selection_tree"]
    assert tree["kept"] == int((objects.group == "kept").sum())
    assert (out / "07_review" / "review_deck.pptx").is_file()

    # refuse to overwrite without --overwrite, succeed with it
    assert main(["select", "-c", str(config_file)]) == 1
    assert main(["select", "-c", str(config_file), "--overwrite"]) == 0

    # curation: no CSV configured -> skip, exit 0, no output folder
    assert main(["curate", "-c", str(config_file), "--yes"]) == 0
    assert not (out / "08_curate").exists()

    # curation with a CSV: delete the first kept object of the first image
    kept = objects[objects.group == "kept"].iloc[0]
    edits = tmp_path / "edits.csv"
    row = {"operation": "delete", "image": kept.image, "labels": str(int(kept.label)), "target": ""}
    pd.DataFrame([row]).to_csv(edits, index=False)
    text = config_file.read_text() + f"curate:\n  edits_csv: {edits}\n"
    config_file.write_text(text)
    assert main(["curate", "-c", str(config_file), "--skip"]) == 0
    assert not (out / "08_curate").exists()
    assert main(["curate", "-c", str(config_file), "--yes"]) == 0
    cells6 = pd.read_csv(out / "06_select" / "cells.csv")
    cells8 = pd.read_csv(out / "08_curate" / "cells.csv")
    assert len(cells8) == len(cells6) - 1
    assert _manifest(out, "08_curate")["status"] == "applied"

    # stats (no VAMPIRE output -> composition skipped)
    assert main(["stats", "-c", str(config_file)]) == 0
    summ = pd.read_csv(out / "10_stats" / "summary_by_condition.csv")
    assert set(summ.metric) == {"area_px", "aspect_ratio"}
    assert summ.loc[summ.condition == "CTRL", "p_welch_vs_reference"].isna().all()
    for name in ("box_area_px", "box_aspect_ratio"):
        for ext in (".png", ".svg"):
            assert (out / "10_stats" / "figures" / f"{name}{ext}").is_file()
        src = pd.read_csv(out / "10_stats" / "figures" / f"source_{name}.csv")
        dots = src[src.row_type == "object_dot"]
        assert len(dots) == len(cells8)  # one plotted dot per object, no per-image aggregation
    assert _manifest(out, "10_stats")["parameters"]["input_tables_from_step"] == 8

    # pixel size from the field width: area_um2 uses the exact derived value
    config_file.write_text(config_file.read_text().replace(
        "stats:\n", "stats:\n  pixel_calibration: {field_width_um: 2485.886, image_width_px: 2560, binning: 4}\n", 1))
    assert main(["stats", "-c", str(config_file), "--overwrite"]) == 0
    um = 2485.886 / 2560 / 4
    metrics = pd.read_csv(out / "10_stats" / "object_metrics.csv")
    assert np.allclose(metrics.area_um2.to_numpy(), metrics.area_px.to_numpy() * um**2, rtol=1e-12, atol=0)
    assert _manifest(out, "10_stats")["parameters"]["pixel_size_um_effective"] == um


def test_bh_known_values() -> None:
    q = bh(np.array([0.01, 0.04, 0.03, np.nan]))
    assert np.allclose(q[:3], [0.03, 0.04, 0.04]) and np.isnan(q[3])


def test_elbow_suggestions() -> None:
    k = np.arange(2, 11)
    inertia = np.array([100, 60, 40, 35, 31, 28, 26, 24.5, 23.0])
    k_sd, k_kn, sd, kd = elbow_suggestions(k, inertia)
    assert k_sd == 3 and k_kn == 4


def test_display_mapping_by_aspect_ratio() -> None:
    cells = pd.DataFrame({"raw_shape_mode": [1, 1, 2, 3], "aspect_ratio": [3.0, 3.2, 1.1, 2.0], "label": [1, 2, 3, 4]})
    m = display_mapping(cells, 3, "aspect_ratio").set_index("raw_shape_mode").display_mode.to_dict()
    assert m == {1: 3, 2: 1, 3: 2}


def test_vampire_runner_is_python38_compatible_and_light() -> None:
    src = (REPO / "src" / "morphoagent" / "pipeline" / "vampire_runner.py").read_text()
    tree = ast.parse(src, feature_version=(3, 8))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    assert top <= {"__future__", "json", "os", "sys", "time", "pathlib", "numpy", "pandas", "PIL"}


def test_cli_parser_and_init_config(tmp_path: Path) -> None:
    p = build_parser()
    a = p.parse_args(["run", "-c", "x.yaml", "--from", "6", "--to", "8", "--skip"])
    assert (a.start, a.end, a.skip) == (6, 8, True)
    target = tmp_path / "cfg.yaml"
    assert main(["init-config", str(target)]) == 0 and target.is_file()
    assert main(["init-config", str(target)]) == 2  # never overwrites
