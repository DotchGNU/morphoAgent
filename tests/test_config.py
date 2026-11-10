from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from morphoagent.pipeline.config import Config, ConfigError, config_to_dict, load_config, pixel_size_um

from .conftest import REPO


def test_default_yaml_equals_dataclass_defaults() -> None:
    shipped = yaml.safe_load((REPO / "configs" / "default.yaml").read_text())
    assert shipped == config_to_dict(Config())


def test_example_yaml_loads() -> None:
    cfg = load_config(REPO / "configs" / "example.yaml")
    assert cfg.enhance.preset == "figure-default"


def test_production_defaults() -> None:
    s = Config().segment
    assert (s.model, s.flow_threshold, s.cellprob_threshold, s.diameter, s.downscale) == ("cpsam", 0.8, -1.0, None, 0.125)
    sel = Config().select
    assert (sel.area_min_px, sel.frame_contact_px, sel.image_trim_fraction) == (2000.0, 24.0, 0.0)
    assert Config().vampire.k == [4] and Config().vampire.n_coordinates == 50


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("segment:\n  flow_treshold: 0.4\n")
    with pytest.raises(ConfigError, match="flow_treshold"):
        load_config(p)


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "c.yaml"
    p.parent.mkdir()
    p.write_text("output_dir: out\ninput:\n  image_dir: ../imgs\n")
    cfg = load_config(p)
    assert cfg.output_dir == str((tmp_path / "sub" / "out").resolve())
    assert cfg.input.image_dir == str((tmp_path / "imgs").resolve())


@pytest.mark.parametrize(
    "text",
    [
        "enhance:\n  preset: magic\n",
        "segment:\n  downscale: 0\n",
        "select:\n  image_trim_fraction: 1.0\n",
        "vampire:\n  k: [1]\n",
        "stats:\n  significance_from: z\n",
        "stats:\n  pixel_calibration: {field_width_um: 100.0}\n",
        "stats:\n  pixel_calibration: {field_width_um: 100.0, image_width_px: 400, binning: 0}\n",
        "stats:\n  pixel_size_um: 0.25\n  pixel_calibration: {field_width_um: 100.0, image_width_px: 400}\n",
        "stats:\n  pixel_size_um: -1\n",
    ],
)
def test_invalid_values(tmp_path: Path, text: str) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(text)
    with pytest.raises(ConfigError):
        load_config(p)


def test_pixel_size_number_or_calibration(tmp_path: Path) -> None:
    assert pixel_size_um(Config()) is None
    p = tmp_path / "c.yaml"
    p.write_text("stats:\n  pixel_size_um: 0.25\n")
    assert pixel_size_um(load_config(p)) == 0.25
    p.write_text("stats:\n  pixel_calibration: {field_width_um: 2485.886, image_width_px: 2560, binning: 4}\n")
    value = pixel_size_um(load_config(p))
    assert value == 2485.886 / 2560 / 4  # same expression and order as written, not a rounded constant
    assert value != 0.24276
    p.write_text("stats:\n  pixel_calibration: {field_width_um: 100, image_width_px: 400}\n")
    assert pixel_size_um(load_config(p)) == 0.25
