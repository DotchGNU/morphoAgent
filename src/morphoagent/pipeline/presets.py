"""Contrast / brightness enhancement presets and preview metrics.

Presets (name -> function)
--------------------------
figure-default
    An empirical sharpen + tone preset fitted to reproduce a reference
    picture-correction (sharpness 100%, brightness +20%, contrast +50%); fit
    residual about 5 grey levels; not an exact re-implementation of any
    software. Pipeline: Lanczos resize to ``output_size`` -> shift the image
    median to ``median_target`` -> radially symmetric sharpening kernel (13
    ring weights, radius 12 px of the resized image) -> piecewise-linear tone
    curve, clip to 0-255, round.
tone-unsharp
    Percentile tone map (the image's own percentiles -> fixed grey levels)
    followed by an unsharp mask whose amount is chosen so that an edge metric at
    display scale reaches a target (or a fixed amount).
clahe
    scikit-image ``equalize_adapthist``.
none
    The grey image unchanged.

Enhanced images are for display and review only; segmentation (step 5) always
runs on the raw grey image.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from importlib import resources
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from .config import ClaheParams, EnhanceConfig, FigureDefaultParams, ToneUnsharpParams


# --------------------------------------------------------------------------- #
# figure-default
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4)
def _load_fit(kernel_path: str | None, lut_path: str | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = resources.files("morphoagent.pipeline.data")
    with resources.as_file(data / "figure_kernel.npy") as p:
        w = np.load(kernel_path or p)
    with resources.as_file(data / "figure_tone_lut.npy") as p:
        xk, yk = np.load(lut_path or p)
    return w, xk, yk


def ring_masks(radius: int) -> list[np.ndarray]:
    """Binary ring masks ``r = 0..radius``; ring ``r`` covers distances in ``[r-0.5, r+0.5)`` px.

    Ring 0 is the centre pixel only. Masks are float arrays of shape
    ``(2*radius+1, 2*radius+1)`` with values 0 or 1.
    """
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    d = np.hypot(yy, xx)
    return [(((d >= r - 0.5) & (d < r + 0.5)) if r > 0 else (d < 0.5)).astype(float) for r in range(radius + 1)]


def figure_default(gray: np.ndarray, p: FigureDefaultParams) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply the ``figure-default`` preset.

    Parameters
    ----------
    gray : numpy.ndarray
        ``uint8`` grey image.
    p : FigureDefaultParams
        ``output_size`` [width, height] in px (or ``None`` to keep the size; the
        kernel radius is defined in px of the output image), ``median_target`` in
        grey levels, optional replacement kernel / tone-curve files.

    Returns
    -------
    (numpy.ndarray, dict)
        ``uint8`` enhanced image and information (median before the shift).

    Notes
    -----
    ``u = sum_r w_r * ringsum_r(img)`` with binary ring masks (correlation with
    reflect padding); the fitted weights per pixel sum to 1. The tone curve is
    ``numpy.interp`` through the fitted knots.
    """
    w, xk, yk = _load_fit(p.kernel_path, p.tone_lut_path)
    radius = len(w) - 1
    masks = ring_masks(radius)
    if p.output_size:
        a = np.array(Image.fromarray(gray).resize(tuple(p.output_size), Image.Resampling.LANCZOS)).astype(float)
    else:
        a = gray.astype(float)
    med = float(np.median(a))
    sh = a + (p.median_target - med)
    u = sum(w[r] * ndi.correlate(sh, masks[r], mode="reflect") for r in range(radius + 1))
    out = np.clip(np.round(np.interp(u, xk, yk)), 0, 255).astype(np.uint8)
    return out, {"median_before_shift": round(med, 3), "median_shift": round(p.median_target - med, 3)}


# --------------------------------------------------------------------------- #
# tone-unsharp
# --------------------------------------------------------------------------- #
def edge_metric(img: np.ndarray, top_fraction: float = 0.02) -> float:
    """Edge strength: mean Sobel magnitude of the strongest ``top_fraction`` pixels / (p99 - p1).

    Dimensionless; larger means crisper edges relative to the image contrast.
    """
    g = img.astype(float)
    m = np.hypot(ndi.sobel(g, 1), ndi.sobel(g, 0))
    thr = np.percentile(m, round(100.0 - 100.0 * top_fraction, 10))
    return float(m[m >= thr].mean() / max(np.percentile(g, 99) - np.percentile(g, 1), 1e-6))


def percentile_tone(gray: np.ndarray, src_pcts: list[float], targets: list[float]) -> np.ndarray:
    """Piecewise-linear map of the image's own percentiles to fixed grey levels (0->0, 255->255)."""
    g = gray.astype(float)
    src = np.percentile(g, src_pcts)
    return np.clip(np.round(np.interp(g, [0, *src, 255.001], [0, *targets, 255])), 0, 255)


def _unsharp(img: np.ndarray, amount: float, sigma: float) -> np.ndarray:
    return np.clip(img + amount * (img - ndi.gaussian_filter(img, sigma)), 0, 255)


def tone_unsharp(gray: np.ndarray, p: ToneUnsharpParams, top_fraction: float = 0.02) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply the ``tone-unsharp`` preset.

    The unsharp amount is ``p.amount`` when given; otherwise the value on the
    grid ``amount_grid = [start, stop, step]`` whose edge metric, measured at
    display scale (height ``display_height_px``), is closest to
    ``base + target_fraction * (reference_edge_metric - base)``.
    Gaussian ``sigma_px`` is in px of the input image.
    """
    toned = percentile_tone(gray, p.source_percentiles, p.target_values)
    h, w = toned.shape
    scale = p.display_height_px / h

    def at_display(x: np.ndarray) -> np.ndarray:
        size = (int(round(w * scale)), int(round(h * scale)))
        return np.array(Image.fromarray(np.clip(x, 0, 255).astype(np.uint8)).resize(size, Image.Resampling.LANCZOS)).astype(float)

    base = edge_metric(at_display(toned), top_fraction)
    info: dict[str, Any] = {"baseline_edge_metric": round(base, 4)}
    if p.amount is not None:
        amount = float(p.amount)
        info["amount_mode"] = "fixed"
    else:
        start, stop, step = p.amount_grid
        grid = np.arange(start, stop + step / 10, step)
        target = base + p.target_fraction * (p.reference_edge_metric - base)
        mets = np.array([edge_metric(at_display(_unsharp(toned, a, p.sigma_px)), top_fraction) for a in grid])
        k = int(np.argmin(np.abs(mets - target)))
        amount = float(grid[k])
        info.update(amount_mode="grid", target_edge_metric=round(target, 4), final_edge_metric=round(float(mets[k]), 4),
                    capped=bool(k == len(grid) - 1))
    info["unsharp_amount"] = round(amount, 3)
    return np.clip(np.round(_unsharp(toned, amount, p.sigma_px)), 0, 255).astype(np.uint8), info


# --------------------------------------------------------------------------- #
# clahe / none
# --------------------------------------------------------------------------- #
def clahe(gray: np.ndarray, p: ClaheParams) -> tuple[np.ndarray, dict[str, Any]]:
    """Contrast-limited adaptive histogram equalisation (skimage), output uint8."""
    from skimage.exposure import equalize_adapthist

    out = equalize_adapthist(gray, kernel_size=p.kernel_size, clip_limit=p.clip_limit, nbins=p.nbins)
    return np.clip(np.round(out * 255), 0, 255).astype(np.uint8), {}


def none(gray: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """Identity preset."""
    return gray.copy(), {}


def get_preset(name: str, ec: EnhanceConfig) -> Callable[[np.ndarray], tuple[np.ndarray, dict[str, Any]]]:
    """Return a one-argument function ``gray -> (enhanced uint8, info)`` for a preset name."""
    if name == "figure-default":
        return lambda g: figure_default(g, ec.figure_default)
    if name == "tone-unsharp":
        return lambda g: tone_unsharp(g, ec.tone_unsharp, ec.edge_top_fraction)
    if name == "clahe":
        return lambda g: clahe(g, ec.clahe)
    if name == "none":
        return none
    raise ValueError(f"unknown preset {name!r}")


# --------------------------------------------------------------------------- #
# Preview metrics
# --------------------------------------------------------------------------- #
def flat_noise(img: np.ndarray, noise_sigma: float = 1.5, grad_sigma: float = 3.0, flat_fraction: float = 0.30) -> float:
    """Flat-background noise in grey levels.

    Standard deviation of ``img - Gaussian(noise_sigma)`` over the
    ``flat_fraction`` of pixels with the lowest gradient magnitude of
    ``Gaussian(grad_sigma)`` (sigmas in px).
    """
    x = img.astype(float)
    gs = ndi.gaussian_filter(x, grad_sigma)
    gm = np.hypot(ndi.sobel(gs, 1), ndi.sobel(gs, 0))
    flat = gm <= np.percentile(gm, round(100.0 * flat_fraction, 10))
    return float((x - ndi.gaussian_filter(x, noise_sigma))[flat].std())


def preview_metrics(img: np.ndarray, ec: EnhanceConfig) -> dict[str, float]:
    """Metrics of an enhanced image after Lanczos resize to ``metrics_width_px``.

    Returns p1/p50/p95/p99 (grey levels), ``edge_strength`` (dimensionless, see
    :func:`edge_metric`) and ``flat_noise`` (grey levels, see :func:`flat_noise`).
    """
    h, w = img.shape
    small = np.array(
        Image.fromarray(img).resize((ec.metrics_width_px, int(round(h * ec.metrics_width_px / w))), Image.Resampling.LANCZOS)
    ).astype(float)
    q = np.percentile(small, (1, 50, 95, 99))
    return {
        "p1": round(float(q[0]), 2), "p50": round(float(q[1]), 2), "p95": round(float(q[2]), 2), "p99": round(float(q[3]), 2),
        "edge_strength": round(edge_metric(small, ec.edge_top_fraction), 4),
        "flat_noise": round(flat_noise(small, ec.noise_sigma_px, ec.flat_gradient_sigma_px, ec.flat_fraction), 3),
    }
