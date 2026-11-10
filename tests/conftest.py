"""Shared fixtures. All test data are synthetic and generated with fixed seeds."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image
from scipy import ndimage as ndi

REPO = Path(__file__).resolve().parents[1]


def synthetic_field(seed: int, shape: tuple[int, int] = (240, 320), n_cells: int = 6, scale: float = 1.0) -> np.ndarray:
    """Grey background with dark-rimmed bright ellipses (a crude phase-contrast look)."""
    rng = np.random.default_rng(seed)
    h, w = shape
    img = np.full(shape, 120.0) + rng.normal(0, 3, shape)
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(n_cells):
        cy, cx = rng.uniform(30, h - 30), rng.uniform(30, w - 30)
        a, b = rng.uniform(10, 24) * scale, rng.uniform(8, 14) * scale
        t = rng.uniform(0, np.pi)
        u = ((xx - cx) * np.cos(t) + (yy - cy) * np.sin(t)) / a
        v = (-(xx - cx) * np.sin(t) + (yy - cy) * np.cos(t)) / b
        r = u**2 + v**2
        img[r < 1] = 190
        img[(r >= 1) & (r < 1.3)] = 60
    r_edge = ((xx - 2) / 14.0) ** 2 + ((yy - h / 2) / 20.0) ** 2  # one object cut by the left frame
    img[r_edge < 1] = 190
    img = ndi.gaussian_filter(img, 0.7)
    return np.clip(np.round(img), 0, 255).astype(np.uint8)


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    """Six synthetic images in two conditions (one with an underscore) plus one all-zero capture."""
    d = tmp_path / "images"
    d.mkdir()
    seed = 0
    for cond in ("CTRL", "DRUG_A"):
        for f in (2, 4, 6):
            seed += 1
            Image.fromarray(synthetic_field(seed)).save(d / f"exp01_{cond}_Image{f:03d}_ch00.png")
    Image.fromarray(np.zeros((240, 320), np.uint8)).save(d / "exp01_CTRL_Image099_ch00.png")
    return d


@pytest.fixture
def config_file(tmp_path: Path, image_dir: Path) -> Path:
    """Small, fast configuration for the synthetic images."""
    cfg = {
        "output_dir": str(tmp_path / "out"),
        "input": {"image_dir": str(image_dir), "strip_prefix": "exp01_", "strip_suffix": "_ch00"},
        "conditions": {"order": ["CTRL", "DRUG_A"], "reference": "CTRL", "colors": {"DRUG_A": "#FF7F7F"}},
        "enhance": {"figure_default": {"output_size": None}, "tone_unsharp": {"amount": 1.0},
                    "metrics_width_px": 160, "sheet_panel_width_px": 120},
        "segment": {"downscale": 0.5, "gpu": False,
                    "tune": {"cellprob_threshold": [-1.0, -2.0], "diameter": [None], "downscale": [0.5], "panel_width_px": 120}},
        "select": {"area_min_px": 150, "frame_contact_px": 5},
        "review": {"font_min_px": 8, "font_max_px": 14, "slide_image_width_px": 320, "kept_linewidth_px": 2,
                   "frame_contact_linewidth_px": 1},
        "stats": {"metrics": ["area_px", "aspect_ratio"], "shannon": {"enabled": True, "n_boot": 50}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class FakeRunner:
    """Threshold-based stand-in for Cellpose (no deep-learning dependency in tests)."""

    def __init__(self, model: str = "cpsam", gpu: bool = False) -> None:
        self.gpu = False

    def __call__(self, img, flow_threshold, cellprob_threshold, diameter):
        lab, _ = ndi.label(img > 150 - 5 * abs(cellprob_threshold))
        return lab.astype(np.int32), 0.0


@pytest.fixture
def fake_cellpose(monkeypatch: pytest.MonkeyPatch) -> None:
    import morphoagent.pipeline.steps_image as si

    monkeypatch.setattr(si, "CellposeRunner", FakeRunner)
