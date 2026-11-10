"""Cellpose-SAM segmentation and object measurement (full resolution, optionally axes on the segmentation grid)."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
from skimage.measure import regionprops_table
from skimage.transform import resize

from .common import LOG

MEASURE_COLUMNS = [
    "label", "area_px", "perimeter_px", "eccentricity", "solidity", "extent", "major_axis_length",
    "minor_axis_length", "aspect_ratio", "form_factor", "border_contact_px", "equiv_diameter_px",
    "centroid_y", "centroid_x", "intensity_mean", "shape_grid",
]
SHAPE_COLUMNS = [
    "area_px", "perimeter_px", "eccentricity", "solidity", "extent", "major_axis_length",
    "minor_axis_length", "aspect_ratio", "form_factor", "border_contact_px", "equiv_diameter_px",
    "centroid_y", "centroid_x", "intensity_mean", "shape_grid",
]
#: Columns replaced by segmentation-grid values when ``measure.shape_grid`` is ``segmentation``.
GRID_COLUMNS = ["major_axis_length", "minor_axis_length", "aspect_ratio", "eccentricity"]
GRID_FULL = "full"
GRID_SEGMENTATION = "segmentation"


def downscale_gray(gray: np.ndarray, factor: float) -> np.ndarray:
    """Shrink a uint8 image by ``factor`` for segmentation.

    Bilinear (``order=1``) with anti-aliasing (scikit-image ``resize``), then
    cast to uint8 by truncation. ``factor == 1`` returns the input unchanged.
    """
    if abs(factor - 1) < 1e-9:
        return gray
    h, w = gray.shape
    out = resize(gray, (int(round(h * factor)), int(round(w * factor))), order=1, anti_aliasing=True, preserve_range=True)
    return out.astype(np.uint8)


def upscale_labels(labels: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour upscaling of a label image to ``shape`` (uint16)."""
    if labels.max() > np.iinfo(np.uint16).max:
        raise ValueError("more than 65535 objects in one image; uint16 label TIFFs cannot hold them")
    if labels.shape == tuple(shape):
        return labels.astype(np.uint16)
    out = resize(labels, shape, order=0, preserve_range=True, anti_aliasing=False)
    return out.astype(np.uint16)


def frame_contact(labels: np.ndarray) -> np.ndarray:
    """Number of pixels of each label lying on the outermost image rows/columns.

    Returns an array indexed by label value (index 0 = background).
    """
    fr = np.zeros(labels.shape, bool)
    fr[0, :] = fr[-1, :] = True
    fr[:, 0] = fr[:, -1] = True
    return np.bincount(labels[fr].ravel(), minlength=int(labels.max()) + 1)


def grid_shape(shape: tuple[int, int], factor: float) -> tuple[int, int]:
    """Size of the segmentation grid: ``round(h * factor), round(w * factor)`` (as in :func:`downscale_gray`)."""
    h, w = shape
    return int(round(h * factor)), int(round(w * factor))


def labels_on_grid(labels: np.ndarray, factor: float) -> np.ndarray:
    """Sample a full-resolution label image on the segmentation grid.

    Grid pixel ``i`` takes the label of full-resolution pixel ``floor((i + 0.5) * H / h)``
    (rows and columns alike). For labels made by :func:`upscale_labels` from a Cellpose
    result on that grid this returns the Cellpose result exactly.

    Parameters
    ----------
    labels : numpy.ndarray
        Full-resolution integer label image (H x W).
    factor : float
        Segmentation downscale factor in (0, 1] (``segment.downscale``).

    Returns
    -------
    numpy.ndarray
        Label image of size :func:`grid_shape` (same dtype).
    """
    big_h, big_w = labels.shape
    h, w = grid_shape(labels.shape, factor)
    ys = np.minimum(((np.arange(h) + 0.5) * big_h / h).astype(np.int64), big_h - 1)
    xs = np.minimum(((np.arange(w) + 0.5) * big_w / w).astype(np.int64), big_w - 1)
    return labels[np.ix_(ys, xs)]


def off_grid_labels(labels: np.ndarray, grid: np.ndarray) -> set[int]:
    """Labels whose full-resolution mask is not the nearest-neighbour upscaling of their grid mask.

    Such objects were drawn or edited at full resolution (for example by hand
    curation) and cannot be measured faithfully on the segmentation grid.
    """
    up = upscale_labels(grid, labels.shape).astype(labels.dtype, copy=False) if grid.max() <= np.iinfo(np.uint16).max \
        else resize(grid, labels.shape, order=0, preserve_range=True, anti_aliasing=False).astype(labels.dtype)
    diff = labels != up
    bad = set(np.unique(labels[diff]).tolist()) | set(np.unique(up[diff]).tolist())
    bad.discard(0)
    return {int(b) for b in bad}


def measure_objects(
    labels: np.ndarray,
    intensity: np.ndarray | None = None,
    shape_grid_factor: float | None = None,
    full_resolution_labels: set[int] | None = None,
) -> pd.DataFrame:
    """Per-object shape measurements on a full-resolution label image.

    Parameters
    ----------
    labels : numpy.ndarray
        Integer label image (0 = background).
    intensity : numpy.ndarray, optional
        Grey image of the same shape for ``intensity_mean`` (grey levels).
    shape_grid_factor : float, optional
        When given (``measure.shape_grid: segmentation``), the axis features
        (:data:`GRID_COLUMNS`: ``major_axis_length``, ``minor_axis_length``,
        ``aspect_ratio``, ``eccentricity``) are measured on the label image
        sampled on the segmentation grid (:func:`labels_on_grid`) and the axis
        lengths are converted back to full-resolution pixels (divided by the
        factor). Objects that are off the grid (:func:`off_grid_labels`), absent
        from it, or listed in ``full_resolution_labels`` keep full-resolution
        values. All other columns are always full resolution.
    full_resolution_labels : set of int, optional
        Labels to measure at full resolution even in grid mode (objects changed
        after segmentation, e.g. by the largest-piece rule).

    Returns
    -------
    pandas.DataFrame
        One row per label: ``area_px`` (px^2), ``perimeter_px`` (px),
        ``eccentricity``, ``solidity``, ``extent`` (dimensionless),
        ``major_axis_length`` / ``minor_axis_length`` (px), ``aspect_ratio`` =
        major / minor, ``form_factor`` = 4*pi*area / perimeter^2,
        ``border_contact_px`` = label pixels on the image frame rows/columns,
        ``equiv_diameter_px`` (px), centroid (px), ``intensity_mean`` and
        ``shape_grid`` (``full`` or ``segmentation``: where the axis features
        were measured).
    """
    lab = labels.astype(np.int32, copy=False)
    if lab.max() == 0:
        return pd.DataFrame(columns=MEASURE_COLUMNS)
    props = ["label", "area", "perimeter", "eccentricity", "solidity", "extent", "major_axis_length",
             "minor_axis_length", "equivalent_diameter_area", "centroid"]
    if intensity is not None:
        props.append("intensity_mean")
    t = pd.DataFrame(regionprops_table(lab, intensity_image=intensity, properties=props))
    t = t.rename(columns={"area": "area_px", "perimeter": "perimeter_px", "equivalent_diameter_area": "equiv_diameter_px",
                          "centroid-0": "centroid_y", "centroid-1": "centroid_x"})
    t["aspect_ratio"] = t.major_axis_length / t.minor_axis_length.replace(0, np.nan)
    t["form_factor"] = 4 * np.pi * t.area_px / t.perimeter_px.replace(0, np.nan) ** 2
    t["border_contact_px"] = frame_contact(lab)[t.label.to_numpy()]
    if "intensity_mean" not in t:
        t["intensity_mean"] = np.nan
    t["shape_grid"] = GRID_FULL
    if shape_grid_factor is not None and abs(shape_grid_factor - 1) > 1e-9:
        if not 0 < shape_grid_factor < 1:
            raise ValueError("shape_grid_factor must be in (0, 1]")
        grid = labels_on_grid(lab, shape_grid_factor)
        g = pd.DataFrame(regionprops_table(grid, properties=("label", "eccentricity", "major_axis_length",
                                                             "minor_axis_length")))
        g["aspect_ratio"] = g.major_axis_length / g.minor_axis_length.replace(0, np.nan)
        g["major_axis_length"] /= shape_grid_factor
        g["minor_axis_length"] /= shape_grid_factor
        skip = off_grid_labels(lab, grid) | set(full_resolution_labels or ())
        g = g[~g.label.isin(skip) & (g.minor_axis_length > 0)].set_index("label")
        use = t.label.isin(g.index).to_numpy()
        t.loc[use, GRID_COLUMNS] = g.loc[t.label[use], GRID_COLUMNS].to_numpy()
        t.loc[use, "shape_grid"] = GRID_SEGMENTATION
    elif shape_grid_factor is not None:
        t["shape_grid"] = GRID_SEGMENTATION
    return t[MEASURE_COLUMNS]


class CellposeRunner:
    """Cellpose model loaded once and evaluated with per-call thresholds.

    Parameters
    ----------
    model : str
        Pretrained model name (``cpsam`` for Cellpose-SAM).
    gpu : bool
        Use CUDA when available; falls back to CPU with a warning.
    """

    def __init__(self, model: str = "cpsam", gpu: bool = True) -> None:
        try:
            import torch
            from cellpose import models
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError("cellpose is not installed; install with `pip install morphoagent[segment]`") from exc
        self.gpu = bool(gpu and torch.cuda.is_available())
        if gpu and not self.gpu:
            LOG.warning("GPU requested but CUDA is not available: running Cellpose on the CPU (slow)")
        LOG.info("loading Cellpose model %r (gpu=%s)", model, self.gpu)
        self.model = models.CellposeModel(gpu=self.gpu, pretrained_model=model)
        self.model_name = model

    def __call__(
        self, img: np.ndarray, flow_threshold: float, cellprob_threshold: float, diameter: float | None
    ) -> tuple[np.ndarray, float]:
        """Segment one uint8 image; returns (int32 labels, runtime in s).

        ``diameter`` (px of the image passed in) is only forwarded when it is
        set; ``None`` leaves Cellpose's own default.
        """
        t0 = time.time()
        kw: dict[str, float] = {"flow_threshold": flow_threshold, "cellprob_threshold": cellprob_threshold}
        if diameter:
            kw["diameter"] = float(diameter)
        out = self.model.eval(img, **kw)
        return np.asarray(out[0]).astype(np.int32), time.time() - t0


def segment_image(
    runner: CellposeRunner, gray: np.ndarray, downscale: float, flow: float, cellprob: float, diameter: float | None
) -> tuple[np.ndarray, float]:
    """Downscale, segment, and upscale the labels back to the original size (uint16)."""
    small = downscale_gray(gray, downscale)
    lab, rt = runner(small, flow, cellprob, diameter)
    return upscale_labels(lab, gray.shape), rt
