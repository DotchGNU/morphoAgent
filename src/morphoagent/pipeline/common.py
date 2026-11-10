"""Shared helpers: logging, step directories, run manifests, image I/O, figures."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import platform
import shutil
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageFont

from . import __version__
from .config import Config

LOG = logging.getLogger("morphoagent")

STEP_DIRS: dict[int, str] = {
    1: "01_load",
    2: "02_enhance-preview",
    3: "03_enhance",
    4: "04_segment-tune",
    5: "05_segment",
    6: "06_select",
    7: "07_review",
    8: "08_curate",
    9: "09_vampire",
    10: "10_stats",
}
MANIFEST = "run_manifest.json"

_PACKAGES = (
    "numpy", "scipy", "pandas", "Pillow", "scikit-image", "matplotlib", "tifffile", "PyYAML",
    "tqdm", "python-pptx", "cellpose", "torch",
)


class OutputExistsError(RuntimeError):
    """A step output directory already holds results and ``--overwrite`` was not given."""


class MissingInputError(RuntimeError):
    """A step needs the output of an earlier step that has not been run."""


@dataclass
class RunContext:
    """Per-invocation options that are not part of the configuration."""

    overwrite: bool = False
    curate_answer: str | None = None  # "yes", "skip" or None (ask interactively)


# --------------------------------------------------------------------------- #
# Logging and progress
# --------------------------------------------------------------------------- #
def setup_logging(level: str = "INFO") -> None:
    """Configure the ``morphoagent`` logger once (console, single-line records)."""
    if LOG.handlers:
        LOG.setLevel(level.upper())
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    LOG.addHandler(h)
    LOG.setLevel(level.upper())
    LOG.propagate = False


def progress(items: Iterable[Any], desc: str, total: int | None = None) -> Iterator[Any]:
    """Wrap an iterable in a single-line ``tqdm`` progress bar (elapsed, rate, remaining)."""
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover - tqdm is a hard dependency
        yield from items
        return
    yield from tqdm(items, desc=desc, total=total, ncols=100, mininterval=0.5, leave=True)


# --------------------------------------------------------------------------- #
# Step directories and manifests
# --------------------------------------------------------------------------- #
def step_dir(cfg: Config, n: int) -> Path:
    """Return the output directory of step ``n`` (not created)."""
    return Path(cfg.output_dir) / STEP_DIRS[n]


def prepare_step_dir(cfg: Config, n: int, overwrite: bool) -> Path:
    """Create the output directory of step ``n``.

    Refuses (``OutputExistsError``) when the directory already contains files,
    unless ``overwrite`` is true, in which case only that step directory is
    removed and recreated.
    """
    d = step_dir(cfg, n)
    if d.exists() and any(d.iterdir()):
        if not overwrite:
            raise OutputExistsError(
                f"{d} already contains results. Re-run with --overwrite to replace them "
                "(only this step directory is removed)."
            )
        root = Path(cfg.output_dir).resolve()
        if d.resolve().parent != root or d.name != STEP_DIRS[n]:
            raise RuntimeError(f"refusing to remove unexpected directory {d}")
        LOG.warning("--overwrite: removing previous results in %s", d)
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def require_step(cfg: Config, n: int) -> Path:
    """Return the directory of a finished step or raise :class:`MissingInputError`."""
    d = step_dir(cfg, n)
    if not (d / MANIFEST).is_file():
        raise MissingInputError(f"step {n} ({STEP_DIRS[n]}) has not been run yet: {d / MANIFEST} is missing")
    return d


def read_manifest(d: Path) -> dict[str, Any]:
    """Read ``run_manifest.json`` from a step directory."""
    with open(d / MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def package_versions() -> dict[str, str]:
    """Versions of the main dependencies that are installed in this environment."""
    out = {"python": platform.python_version(), "morphoagent": __version__}
    for name in _PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return out


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def file_sha256(path: str | Path | None) -> str | None:
    """SHA-256 of a file, or ``None`` when the path is not given."""
    if not path:
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(
    d: Path,
    cfg: Config,
    step: int,
    parameters: dict[str, Any],
    counts_in: dict[str, Any],
    counts_out: dict[str, Any],
    **extra: Any,
) -> Path:
    """Write ``run_manifest.json``: parameters, versions, counts in/out, timestamp."""
    m = {
        "step": step,
        "name": STEP_DIRS[step],
        "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "versions": package_versions(),
        "platform": platform.platform(),
        "config_file": cfg.config_path,
        "config_sha256": file_sha256(cfg.config_path),
        "parameters": parameters,
        "counts_in": counts_in,
        "counts_out": counts_out,
    }
    m.update(extra)
    p = d / MANIFEST
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2, default=_json_default)
    return p


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def load_gray(path: str | Path) -> np.ndarray:
    """Read an image as a single-channel 8-bit array.

    RGB images whose three channels are identical are reduced to one channel
    without any arithmetic; other colour images are converted with Pillow's
    ``convert("L")``. Integer images with more than 8 bits are scaled linearly
    from ``[0, dtype max]`` to ``[0, 255]``; float images are assumed to be in
    ``[0, 1]``.

    Parameters
    ----------
    path : str or Path
        Image file (jpg, png, tif).

    Returns
    -------
    numpy.ndarray
        ``uint8`` array of shape (height, width), grey levels 0-255.
    """
    with Image.open(path) as im:
        a = np.array(im)
        if a.ndim == 3:
            rgb = a[..., :3]
            if (rgb[..., 0] == rgb[..., 1]).all() and (rgb[..., 1] == rgb[..., 2]).all():
                a = rgb[..., 0]
            else:
                a = np.array(im.convert("L"))
    if a.dtype == np.uint8:
        return a
    if np.issubdtype(a.dtype, np.integer):
        scale = 255.0 / float(np.iinfo(a.dtype).max)
    else:
        scale = 255.0
    return np.clip(np.round(a.astype(np.float64) * scale), 0, 255).astype(np.uint8)


def resize_gray(img: np.ndarray, width: int, height: int) -> np.ndarray:
    """Lanczos resize of a uint8 image with Pillow (output uint8)."""
    return np.array(Image.fromarray(img).resize((int(width), int(height)), Image.Resampling.LANCZOS))


def resize_to_width(img: np.ndarray, width: int) -> np.ndarray:
    """Lanczos resize keeping the aspect ratio (uint8 grey or RGB)."""
    h, w = img.shape[:2]
    height = int(round(h * width / w))
    return np.array(Image.fromarray(img).resize((int(width), height), Image.Resampling.LANCZOS))


def get_font(path: str | None, size_px: int) -> ImageFont.FreeTypeFont:
    """Return a TrueType font; ``path=None`` uses matplotlib's bundled DejaVu Sans Bold."""
    if not path:
        from matplotlib import font_manager

        path = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans", weight="bold"))
    return ImageFont.truetype(path, int(size_px))


# --------------------------------------------------------------------------- #
# Tables and inputs shared by several steps
# --------------------------------------------------------------------------- #
def read_inventory(cfg: Config) -> pd.DataFrame:
    """Valid (non-skipped) images from step 1, in inventory order."""
    d = require_step(cfg, 1)
    inv = pd.read_csv(d / "inventory.csv")
    return inv[inv.status == "ok"].reset_index(drop=True)


def condition_order(cfg: Config, present: Iterable[str]) -> list[str]:
    """Order conditions: configured order first (if present), then the rest alphabetically."""
    present = list(dict.fromkeys(present))
    first = [c for c in cfg.conditions.order if c in present]
    rest = sorted(c for c in present if c not in first)
    return first + rest


def reference_condition(cfg: Config, conditions: list[str]) -> str:
    """Configured reference condition, or the first condition in order."""
    ref = cfg.conditions.reference or conditions[0]
    if ref not in conditions:
        raise ValueError(f"reference condition {ref!r} is not among the conditions {conditions}")
    return ref


def sample_images(inv: pd.DataFrame, samples: list[str] | None, max_conditions: int, order: list[str]) -> list[str]:
    """Sample image ids: the configured list, or the first image of up to N conditions."""
    if samples:
        missing = [s for s in samples if s not in set(inv.image)]
        if missing:
            raise ValueError(f"sample image(s) not in the inventory: {missing}")
        return list(samples)
    out = []
    for c in order[:max_conditions]:
        sub = inv[inv.condition == c]
        if len(sub):
            out.append(sub.image.iloc[0])
    return out


def analysis_set(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, int]:
    """Objects, cells and fields tables used downstream of selection.

    Returns the curated tables of step 8 when that step was applied, otherwise
    the tables of step 6, plus the number of the step they came from.
    """
    cur = step_dir(cfg, 8)
    if (cur / MANIFEST).is_file() and read_manifest(cur).get("status") == "applied":
        src, n = cur, 8
    else:
        src, n = require_step(cfg, 6), 6
    objects = pd.read_csv(src / "objects.csv")
    cells = pd.read_csv(src / "cells.csv")
    fields = pd.read_csv(src / "fields.csv")
    return objects, cells, fields, n


def label_path(cfg: Config, rel: str) -> Path:
    """Absolute path of a label TIFF stored relative to ``output_dir`` in a fields table."""
    return Path(cfg.output_dir) / rel
