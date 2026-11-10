"""Field table (select.field_table): chosen fields and per-field label files, on synthetic images."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tifffile
import yaml

from morphoagent.pipeline.cli import main
from morphoagent.pipeline.field_table import FieldTableError, check_label_files, read_field_table, read_label_file
from morphoagent.pipeline.segmentation import measure_objects


def _with_field_table(config_file: Path, table: Path) -> Path:
    cfg = yaml.safe_load(config_file.read_text())
    cfg.setdefault("select", {})["field_table"] = str(table)
    p = config_file.with_name("config_ft.yaml")
    p.write_text(yaml.safe_dump(cfg))
    return p


def _inventory() -> pd.DataFrame:
    return pd.DataFrame({"image": ["A_Image001", "A_Image002", "B_Image001"], "condition": ["A", "A", "B"],
                         "field": ["Image001", "Image002", "Image001"]})


def test_read_field_table_resolves_and_orders(tmp_path: Path) -> None:
    (tmp_path / "lab").mkdir()
    tifffile.imwrite(tmp_path / "lab" / "b1.tif", np.zeros((4, 4), np.uint16))
    t = tmp_path / "fields.csv"
    t.write_text("image,label_file,condition,note\nB_Image001,lab/b1.tif,B,why\nA_Image001,,A,\n")
    ft = read_field_table(t, _inventory())
    assert list(ft.image) == ["A_Image001", "B_Image001"]  # inventory order, not table order
    assert list(ft.label_source) == ["segment", "field_table"]
    assert ft.label_file.iloc[1] == str((tmp_path / "lab" / "b1.tif").resolve())
    assert list(ft.condition) == ["A", "B"] and list(ft.field) == ["Image001", "Image001"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("label_file\nx.tif\n", "missing column"),
        ("image\n\n", "lists no images"),
        ("image\nA_Image001\nA_Image001\n", "more than once"),
        ("image\nC_Image009\n", "not among the valid images"),
        ("image,condition\nA_Image001,B\n", "condition differs"),
        ("image,label_file\nA_Image001,missing.tif\n", "not found"),
    ],
)
def test_read_field_table_errors(tmp_path: Path, text: str, message: str) -> None:
    t = tmp_path / "fields.csv"
    t.write_text(text)
    with pytest.raises(FieldTableError, match=message):
        read_field_table(t, _inventory())


def test_read_label_file_checks(tmp_path: Path) -> None:
    p = tmp_path / "l.tif"
    tifffile.imwrite(p, np.zeros((5, 6), np.uint16))
    assert read_label_file(p, (5, 6)).dtype == np.int32
    with pytest.raises(FieldTableError, match="differs from the image size"):
        read_label_file(p, (6, 5))
    tifffile.imwrite(p, np.zeros((5, 6), np.float32))
    with pytest.raises(FieldTableError, match="must be integers"):
        read_label_file(p, (5, 6))


@pytest.mark.usefixtures("fake_cellpose")
def test_select_with_field_table_mixes_files_and_step5(config_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "1"]) == 0
    assert main(["run", "-c", str(config_file), "--from", "5", "--to", "5"]) == 0
    seg = pd.read_csv(out / "05_segment" / "objects.csv")
    # a hand-edited label file for one field: the largest object of CTRL_Image002 removed, two pieces added
    lab = tifffile.imread(out / "05_segment" / "labels" / "CTRL_Image002_labels.tif").astype(np.int32)
    big = int(seg[seg.image == "CTRL_Image002"].sort_values("area_px").label.iloc[-1])
    edited = lab.copy()
    edited[edited == big] = 0
    edited[100:140, 100:130] = 900          # new object, 40 x 30
    edited[200:202, 300:302] = 901          # tiny object -> below the area minimum
    edits = tmp_path / "edited"
    edits.mkdir()
    tifffile.imwrite(edits / "ctrl2.tif", edited.astype(np.uint16))
    table = tmp_path / "fields.csv"
    table.write_text("image,label_file,condition\nCTRL_Image002,edited/ctrl2.tif,CTRL\nDRUG_A_Image004,,DRUG_A\n")
    cfg = _with_field_table(config_file, table)
    assert main(["run", "-c", str(cfg), "--from", "6", "--to", "6"]) == 0
    fields = pd.read_csv(out / "06_select" / "fields.csv", keep_default_na=False)
    assert list(fields.image) == ["CTRL_Image002", "DRUG_A_Image004"]
    assert list(fields.label_source) == ["field_table", "segment"]
    assert fields.label_input.iloc[0] == str((edits / "ctrl2.tif").resolve())
    objects = pd.read_csv(out / "06_select" / "objects.csv")
    o2 = objects[objects.image == "CTRL_Image002"].set_index("label")
    assert big not in o2.index and {900, 901} <= set(o2.index)
    expect = measure_objects(edited).set_index("label")
    assert o2.loc[900, "area_px"] == expect.loc[900, "area_px"] == 1200
    assert o2.loc[901, "group"] == "below_area_min"
    # the step-5 field keeps its step-5 measurements
    o4 = objects[objects.image == "DRUG_A_Image004"].set_index("label")
    s4 = seg[seg.image == "DRUG_A_Image004"].set_index("label")
    assert o4.area_px.to_dict() == s4.area_px.to_dict()
    written = tifffile.imread(out / "06_select" / "labels" / "CTRL_Image002_labels.tif")
    assert written.dtype == np.uint16 and np.array_equal(written, edited)
    man = json.loads((out / "06_select" / "run_manifest.json").read_text())
    ft = man["field_table"]
    assert ft["fields"] == 2 and ft["fields_with_label_file"] == 1 and ft["fields_from_step5"] == 1
    assert ft["valid_images_not_listed"] == 4 and len(ft["sha256"]) == 64
    assert man["selection_tree"]["kept"] == int((objects.group == "kept").sum())
    assert (edits / "ctrl2.tif").stat().st_size > 0  # the input file is left in place


def test_select_with_only_label_files_needs_no_step5(config_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "1"]) == 0
    lab = np.zeros((240, 320), np.uint16)
    lab[50:120, 60:100] = 3
    lab[0:40, 200:260] = 4  # 60 px on the top frame -> frame_contact
    tifffile.imwrite(tmp_path / "only.tif", lab)
    table = tmp_path / "fields.csv"
    table.write_text(f"image,label_file\nDRUG_A_Image006,{tmp_path / 'only.tif'}\n")
    cfg = _with_field_table(config_file, table)
    assert main(["run", "-c", str(cfg), "--from", "6", "--to", "6"]) == 0
    assert not (out / "05_segment").exists()
    objects = pd.read_csv(out / "06_select" / "objects.csv").set_index("label")
    assert objects.loc[3, "group"] == "kept" and objects.loc[4, "group"] == "frame_contact"
    assert objects.loc[3, "condition"] == "DRUG_A"


def test_select_field_table_rejects_wrong_size(config_file: Path, tmp_path: Path) -> None:
    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "1"]) == 0
    tifffile.imwrite(tmp_path / "small.tif", np.zeros((24, 32), np.uint16))
    table = tmp_path / "fields.csv"
    table.write_text("image,label_file\nCTRL_Image002,small.tif\n")
    cfg = _with_field_table(config_file, table)
    assert main(["run", "-c", str(cfg), "--from", "6", "--to", "6"]) == 1  # FieldTableError is reported, exit 1
    assert not (tmp_path / "out" / "06_select").exists()  # checked before the step folder is created
    inv = pd.read_csv(tmp_path / "out" / "01_load" / "inventory.csv")
    ft = read_field_table(table, inv[inv.status == "ok"])
    with pytest.raises(FieldTableError, match="differs from the image size"):
        check_label_files(ft, inv[inv.status == "ok"])
