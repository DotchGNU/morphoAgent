from __future__ import annotations

import numpy as np

from morphoagent.pipeline.config import ClaheParams, EnhanceConfig, FigureDefaultParams, ToneUnsharpParams
from morphoagent.pipeline.presets import (
    _load_fit,
    clahe,
    edge_metric,
    figure_default,
    flat_noise,
    get_preset,
    percentile_tone,
    preview_metrics,
    ring_masks,
    tone_unsharp,
)

from .conftest import synthetic_field


def test_ring_masks_partition_the_disc() -> None:
    masks = ring_masks(12)
    total = np.sum(masks, axis=0)
    assert total.max() == 1  # rings are disjoint
    assert masks[0].sum() == 1 and masks[0][12, 12] == 1  # ring 0 = centre pixel only
    yy, xx = np.mgrid[-12:13, -12:13]
    assert np.array_equal(total.astype(bool), np.hypot(yy, xx) < 12.5)


def test_packaged_kernel_weights_sum_to_one_per_pixel() -> None:
    w, xk, yk = _load_fit(None, None)
    assert w.shape == (13,)
    n = np.array([m.sum() for m in ring_masks(12)])
    assert abs(float((w * n).sum()) - 1.0) < 1e-9
    assert np.all(np.diff(xk) > 0) and np.all(np.diff(yk) >= 0)


def test_figure_default_shape_and_median_shift() -> None:
    g = synthetic_field(1)
    out, info = figure_default(g, FigureDefaultParams(output_size=[160, 120]))
    assert out.shape == (120, 160) and out.dtype == np.uint8
    out2, _ = figure_default(g, FigureDefaultParams(output_size=None))
    assert out2.shape == g.shape
    assert "median_shift" in info


def test_figure_default_is_deterministic() -> None:
    g = synthetic_field(2)
    a, _ = figure_default(g, FigureDefaultParams(output_size=None))
    b, _ = figure_default(g, FigureDefaultParams(output_size=None))
    assert np.array_equal(a, b)


def test_percentile_tone_maps_percentiles_to_targets() -> None:
    rng = np.random.default_rng(0)
    g = rng.integers(40, 200, (100, 100)).astype(np.uint8)
    t = percentile_tone(g, [1, 50, 95, 99], [62, 179, 235, 255])
    assert abs(np.percentile(t, 50) - 179) <= 2


def test_tone_unsharp_fixed_and_grid() -> None:
    g = synthetic_field(3)
    out, info = tone_unsharp(g, ToneUnsharpParams(amount=2.0, display_height_px=120))
    assert info["amount_mode"] == "fixed" and out.dtype == np.uint8
    out, info = tone_unsharp(g, ToneUnsharpParams(amount_grid=[0.0, 2.0, 0.5], display_height_px=120))
    assert info["amount_mode"] == "grid" and 0.0 <= info["unsharp_amount"] <= 2.0


def test_clahe_and_none() -> None:
    g = synthetic_field(4)
    out, _ = clahe(g, ClaheParams())
    assert out.shape == g.shape and out.dtype == np.uint8
    ident, _ = get_preset("none", EnhanceConfig())(g)
    assert np.array_equal(ident, g)


def test_metrics() -> None:
    flat = np.full((64, 64), 100, np.uint8)
    assert flat_noise(flat) == 0.0
    g = synthetic_field(5)
    assert edge_metric(g) > 0
    m = preview_metrics(g, EnhanceConfig(metrics_width_px=160))
    assert set(m) == {"p1", "p50", "p95", "p99", "edge_strength", "flat_noise"}
    assert m["p1"] <= m["p50"] <= m["p95"] <= m["p99"]
