"""Assemble the single-file review page (``morphoagent ui build``)."""

from __future__ import annotations

import datetime as _dt
import json
import platform
from importlib import resources
from pathlib import Path
from typing import Any

from ..common import LOG, OutputExistsError, file_sha256, package_versions
from ..config import Config
from .dataset import build_dataset

PLACEHOLDERS = ("/*__BFM_CSS__*/", "/*__BFM_JS__*/", "__BFM_DATA__")


def static_text(name: str) -> str:
    """Text of a packaged file in ``morphoagent/pipeline/ui/static``."""
    return (resources.files("morphoagent.pipeline.ui") / "static" / name).read_text(encoding="utf-8")


def json_for_script(doc: dict[str, Any]) -> str:
    """JSON text that is safe inside ``<script type="application/json">`` (no ``</`` or ``<!--``)."""
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def render_page(doc: dict[str, Any]) -> str:
    """Return the complete HTML page with CSS, JavaScript and the data document inlined."""
    page = static_text("page.html")
    for ph in PLACEHOLDERS:
        if page.count(ph) != 1:
            raise RuntimeError(f"page template must contain {ph} exactly once")
    return (page.replace(PLACEHOLDERS[0], static_text("app.css"))
                .replace(PLACEHOLDERS[1], static_text("app.js"))
                .replace(PLACEHOLDERS[2], json_for_script(doc)))


def default_html_path(cfg: Config) -> Path:
    """``ui.html_path`` or ``<output_dir>/ui/review.html``."""
    return Path(cfg.ui.html_path) if cfg.ui.html_path else Path(cfg.output_dir) / "ui" / "review.html"


def run_ui_build(cfg: Config, out: str | Path | None = None, overwrite: bool = False) -> Path:
    """Build the offline review page and a manifest next to it.

    Parameters
    ----------
    cfg : Config
        Loaded configuration (tabs of steps that have not been run show a note).
    out : str or Path, optional
        Output HTML file; default :func:`default_html_path`.
    overwrite : bool
        Replace an existing file (otherwise :class:`OutputExistsError`).

    Returns
    -------
    Path
        The written HTML file. ``<stem>_manifest.json`` is written beside it.
    """
    path = Path(out) if out else default_html_path(cfg)
    manifest = path.with_name(f"{path.stem}_manifest.json")
    if path.exists() and not overwrite:
        raise OutputExistsError(f"{path} already exists. Re-run with --overwrite to replace it.")
    doc = build_dataset(cfg, live=False)
    html = render_page(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)
    size = path.stat().st_size
    m = {
        "tool": "ui build",
        "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "versions": package_versions(),
        "platform": platform.platform(),
        "config_file": cfg.config_path,
        "config_sha256": file_sha256(cfg.config_path),
        "parameters": {k: getattr(cfg.ui, k) for k in ("max_width", "jpeg_quality", "preview_width_px", "median_target_grid")},
        "counts_out": {"fields": len(doc["fields"]), "objects": sum(len(f["objects"]) for f in doc["fields"]),
                       "enhance_samples": len(doc["enhance"]["samples"]),
                       "enhance_variants": sum(len(s["variants"]) for s in doc["enhance"]["samples"]),
                       "segment_samples": len(doc["segment"]["samples"]),
                       "proposals": len(doc["decisions"]["proposals"]), "html_bytes": size},
        "html_file": path.name,
        "html_sha256": file_sha256(path),
        "dataset_id": doc["dataset_id"],
    }
    manifest.write_text(json.dumps(m, indent=2), encoding="utf-8")
    LOG.info("ui build done: %s (%.1f MB, %d fields)", path, size / 1e6, len(doc["fields"]))
    return path
