"""measure.shape_grid: axis features on the full-resolution labels or on the segmentation grid (synthetic data)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tifffile
import yaml
from skimage.draw import ellipse
from skimage.measure import regionprops_table

from morphoagent.pipeline.cli import main
from morphoagent.pipeline.config import ConfigError, load_config, shape_grid_factor
from morphoagent.pipeline.segmentation import (
    GRID_COLUMNS,
    labels_on_grid,
    measure_objects,
    off_grid_labels,
    upscale_labels,
)

FACTOR = 0.125


def _small_labels() -> np.ndarray:
    """Two thin tilted ellipses and one round object on a 40 x 50 grid (as Cellpose would return them)."""
    small = np.zeros((40, 50), np.int32)
    for lab, (r, c, a, b, rot) in enumerate([(12, 14, 9, 2.5, 0.5), (26, 34, 11, 3, -0.9), (30, 10, 5, 5, 0.0)], start=1):
        rr, cc = ellipse(r, c, a, b, shape=small.shape, rotation=rot)
        small[rr, cc] = lab
    return small


def _with(config_file: Path, **sections: dict) -> Path:
    cfg = yaml.safe_load(config_file.read_text())
    for k, v in sections.items():
        cfg.setdefault(k, {}).update(v)
    p = config_file.with_name("config_grid.yaml")
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_labels_on_grid_inverts_nearest_upscaling() -> None:
    small = _small_labels()
    full = upscale_labels(small, (320, 400))
    assert np.array_equal(labels_on_grid(full, FACTOR), small)
    assert off_grid_labels(full.astype(np.int32), labels_on_grid(full, FACTOR)) == set()


def test_full_mode_measures_on_full_resolution() -> None:
    full = upscale_labels(_small_labels(), (320, 400)).astype(np.int32)
    m = measure_objects(full).set_index("label")
    ref = pd.DataFrame(regionprops_table(full, properties=("label", "major_axis_length", "minor_axis_length",
                                                           "eccentricity"))).set_index("label")
    assert np.allclose(m.major_axis_length, ref.major_axis_length)
    assert np.allclose(m.aspect_ratio, ref.major_axis_length / ref.minor_axis_length)
    assert set(m.shape_grid) == {"full"}


def test_segmentation_mode_equals_grid_measurement_scaled_back() -> None:
    small = _small_labels()
    full = upscale_labels(small, (320, 400)).astype(np.int32)
    g = pd.DataFrame(regionprops_table(small, properties=("label", "major_axis_length", "minor_axis_length",
                                                          "eccentricity"))).set_index("label")
    m = measure_objects(full, shape_grid_factor=FACTOR).set_index("label")
    mf = measure_objects(full).set_index("label")
    assert np.allclose(m.aspect_ratio, g.major_axis_length / g.minor_axis_length, rtol=0, atol=1e-12)
    assert np.allclose(m.major_axis_length, g.major_axis_length / FACTOR, rtol=0, atol=1e-9)
    assert np.allclose(m.minor_axis_length, g.minor_axis_length / FACTOR, rtol=0, atol=1e-9)
    assert np.allclose(m.eccentricity, g.eccentricity, rtol=0, atol=1e-12)
    assert set(m.shape_grid) == {"segmentation"}
    # the thin objects differ between the two grids; area and perimeter stay full resolution in both modes
    assert (m.aspect_ratio - mf.aspect_ratio).abs().max() > 1e-3
    for col in ("area_px", "perimeter_px", "solidity", "extent", "form_factor", "border_contact_px"):
        assert np.array_equal(m[col].to_numpy(), mf[col].to_numpy())


def test_off_grid_and_listed_objects_stay_full_resolution() -> None:
    full = upscale_labels(_small_labels(), (320, 400)).astype(np.int32)
    full[full == 2] = 0
    rr, cc = ellipse(210, 270, 70, 21, shape=full.shape, rotation=-0.9)  # drawn at full resolution
    full[rr, cc] = 2
    m = measure_objects(full, shape_grid_factor=FACTOR, full_resolution_labels={3}).set_index("label")
    mf = measure_objects(full).set_index("label")
    assert m.loc[1, "shape_grid"] == "segmentation"
    assert m.loc[2, "shape_grid"] == "full" and m.loc[3, "shape_grid"] == "full"
    for lab in (2, 3):
        assert np.allclose(m.loc[lab, GRID_COLUMNS].astype(float), mf.loc[lab, GRID_COLUMNS].astype(float))


def test_config_option_and_validation(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert cfg.measure.shape_grid == "full" and shape_grid_factor(cfg) is None
    cfg = load_config(_with(config_file, measure={"shape_grid": "segmentation"}))
    assert shape_grid_factor(cfg) == cfg.segment.downscale
    with pytest.raises(ConfigError, match="measure.shape_grid"):
        load_config(_with(config_file, measure={"shape_grid": "half"}))


@pytest.mark.usefixtures("fake_cellpose")
def test_pipeline_modes_and_field_table_overrides(config_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    seg_cfg = _with(config_file, measure={"shape_grid": "segmentation"})
    assert main(["run", "-c", str(seg_cfg), "--from", "1", "--to", "1"]) == 0
    assert main(["run", "-c", str(seg_cfg), "--from", "5", "--to", "6"]) == 0
    o5 = pd.read_csv(out / "05_segment" / "objects.csv")
    o6 = pd.read_csv(out / "06_select" / "objects.csv")
    factor = 0.5  # segment.downscale of the test configuration
    # step 5: every object segmented on the grid is measured there
    assert set(o5.shape_grid) == {"segmentation"}
    lab = tifffile.imread(out / "05_segment" / "labels" / "CTRL_Image002_labels.tif").astype(np.int32)
    g = pd.DataFrame(regionprops_table(labels_on_grid(lab, factor), properties=("label", "major_axis_length",
                                                                                "minor_axis_length"))).set_index("label")
    mine = o5[o5.image == "CTRL_Image002"].set_index("label")
    big = g.index[g.minor_axis_length > 0]
    assert np.allclose(mine.loc[big, "aspect_ratio"], (g.major_axis_length / g.minor_axis_length).loc[big])
    # step 6: objects cut by the largest-piece rule are measured at full resolution, the rest on the grid
    frag = o6.fragment_px_removed > 0
    assert set(o6.loc[frag, "shape_grid"]) <= {"full"}
    assert (o6.loc[~frag, "shape_grid"] == "segmentation").all()

    # field table: per-field shape_grid override and a segmentation_label_file
    edited = lab.copy()
    target = int(mine.area_px.idxmax())
    edited[(edited == target) & (np.arange(edited.shape[1])[None, :] % 2 == 0)] = 0  # edited at full resolution
    tifffile.imwrite(tmp_path / "edited.tif", edited.astype(np.uint16))
    seg_file = out / "05_segment" / "labels" / "CTRL_Image002_labels.tif"
    table = tmp_path / "fields.csv"
    table.write_text("image,label_file,shape_grid,segmentation_label_file\n"
                     f"CTRL_Image002,{tmp_path / 'edited.tif'},,{seg_file}\n"
                     "CTRL_Image004,,full,\n"
                     "DRUG_A_Image002,,,\n")
    ft_cfg = _with(seg_cfg, select={"field_table": str(table)})
    ft = yaml.safe_load(ft_cfg.read_text())
    ft["output_dir"] = str(tmp_path / "out_ft")
    ft_cfg.write_text(yaml.safe_dump(ft))
    import shutil

    shutil.copytree(out / "01_load", tmp_path / "out_ft" / "01_load")
    shutil.copytree(out / "05_segment", tmp_path / "out_ft" / "05_segment")
    assert main(["run", "-c", str(ft_cfg), "--from", "6", "--to", "6"]) == 0
    t = pd.read_csv(tmp_path / "out_ft" / "06_select" / "objects.csv")
    fields = pd.read_csv(tmp_path / "out_ft" / "06_select" / "fields.csv").set_index("image")
    assert fields.loc["CTRL_Image004", "shape_grid"] == "full"
    assert set(t[t.image == "CTRL_Image004"].shape_grid) == {"full"}
    e = t[t.image == "CTRL_Image002"].set_index("label")
    assert e.loc[target, "shape_grid"] == "full"  # changed against the segmentation output
    other = [lb for lb in e.index if lb != target and e.loc[lb, "fragment_px_removed"] == 0]
    assert other and (e.loc[other, "shape_grid"] == "segmentation").all()


def test_field_table_rejects_unknown_shape_grid(tmp_path: Path) -> None:
    from morphoagent.pipeline.field_table import FieldTableError, read_field_table

    inv = pd.DataFrame({"image": ["A_Image001"], "condition": ["A"], "field": ["Image001"]})
    t = tmp_path / "fields.csv"
    t.write_text("image,shape_grid\nA_Image001,coarse\n")
    with pytest.raises(FieldTableError, match="shape_grid"):
        read_field_table(t, inv)


def test_changed_labels_detects_gained_and_lost_pixels() -> None:
    from morphoagent.pipeline.steps_objects import _changed_labels

    seg = upscale_labels(_small_labels(), (320, 400)).astype(np.int32)
    lab = seg.copy()
    lab[(lab == 1) & (np.arange(320)[:, None] < 96)] = 0  # label 1 loses whole grid blocks (still on the grid)
    lab[0:8, 0:8] = 3  # label 3 gains one block
    assert _changed_labels(lab, seg) == {1, 3}
    assert off_grid_labels(lab, labels_on_grid(lab, FACTOR)) == set()  # only the comparison with seg can see it
