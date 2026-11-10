"""Rule-based object selection (step 6) and the shared group rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import ndimage as ndi

GROUP_KEPT = "kept"
GROUP_FRAME = "frame_contact"
GROUP_SMALL = "below_area_min"
GROUP_TRIM = "image_trimmed"


def structure(connectivity: int) -> np.ndarray:
    """3x3 structuring element for 4- or 8-connectivity."""
    return np.ones((3, 3), bool) if connectivity == 8 else ndi.generate_binary_structure(2, 1)


def largest_piece(labels: np.ndarray, connectivity: int = 8) -> tuple[np.ndarray, dict[int, int]]:
    """Keep only the largest connected piece of every label.

    Parameters
    ----------
    labels : numpy.ndarray
        Integer label image.
    connectivity : int
        4 or 8 (pixel connectivity used to find pieces).

    Returns
    -------
    (numpy.ndarray, dict)
        Cleaned label image (same dtype) and ``{label: pixels removed}`` for the
        labels that had more than one piece. Ties keep the first piece in
        scan order.
    """
    out = labels.copy()
    removed: dict[int, int] = {}
    st = structure(connectivity)
    objs = ndi.find_objects(labels.astype(np.int64))
    for i, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        m = labels[sl] == i
        cc, n = ndi.label(m, structure=st)
        if n <= 1:
            continue
        sizes = ndi.sum(m, cc, range(1, n + 1))
        keep = cc == (int(np.argmax(sizes)) + 1)
        drop = m & ~keep
        removed[i] = int(drop.sum())
        view = out[sl]
        view[drop] = 0
    return out, removed


def image_trim_mask(objects: pd.DataFrame, fraction: float, features: list[str], log_area: bool,
                    min_images: int) -> pd.Series:
    """Drop the ``fraction`` most atypical images within each condition.

    Per-image vector = median of each feature (and median log10 area when
    ``log_area``) over all objects of the image; the vectors are z-scored over
    all images (population SD; a zero SD is replaced by 1) and the Euclidean
    distance to the condition's own centroid is computed. Within each condition
    the ``floor(n_images * (1 - fraction))`` closest images are kept, but never
    fewer than ``min_images``.

    Returns
    -------
    pandas.Series
        Boolean mask aligned to ``objects.index`` (True = image kept).
    """
    if fraction <= 0:
        return pd.Series(True, index=objects.index)
    d = objects.copy()
    feats = list(features)
    if log_area:
        d["log10_area"] = np.log10(d.area_px)
        feats.append("log10_area")
    im = d.groupby(["condition", "image"])[feats].median()
    z = (im - im.mean()) / im.std(ddof=0).replace(0, 1)
    keep: list[str] = []
    for _, sub in z.groupby(level=0):
        dist = np.linalg.norm(sub.values - sub.values.mean(0), axis=1)
        k = max(int(np.floor(len(sub) * (1 - fraction))), min_images)
        order = np.argsort(dist)[:k]
        keep += [sub.index[i][1] for i in order]
    return objects.image.isin(set(keep))


def group_rule(area_px: pd.Series, contact_px: pd.Series, area_min: float, contact_max: float) -> np.ndarray:
    """Group of each object by the area and frame-contact rules (in this order)."""
    return np.where(
        area_px < area_min, GROUP_SMALL, np.where(contact_px > contact_max, GROUP_FRAME, GROUP_KEPT)
    )
