"""Snapshot folders: ``snapshots/<UTC time>_<stage>_<slug>/`` with five files.

=============  ==============================================================
``view.png``   the proposal's figure at ``plot.dpi`` (300), transparency kept
``view.svg``   the figure's SVG sibling when it exists; otherwise an SVG that
               embeds ``view.png`` (raster figures such as montages); when the
               proposal has no figure, both are a table of the options drawn
               with matplotlib
``source.csv`` the ``source_<stem>.csv`` of the figure; otherwise the options
               table
``state.json`` figure settings (``figure_state`` of the proposal) and which
               file was used
``note.json``  stage, full proposal, chosen option, comment, time
=============  ==============================================================

Folders are created with ``mkdir(exist_ok=False)``; a name clash gets a
``-2``, ``-3`` ... suffix, so a snapshot is never overwritten.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image

from .. import __version__
from ..pipeline.common import package_versions
from ..pipeline.config import Config
from .protocol import REJECTED, Proposal, compact, records_dir, sibling_source, sibling_svg, slugify, utc_now

SNAPSHOT_FILES = ("view.png", "view.svg", "source.csv", "state.json", "note.json")
STATE_SCHEMA = "morphoagent-snapshot-state/1"
NOTE_SCHEMA = "morphoagent-snapshot-note/1"


def options_table(prop: Proposal, max_number_columns: int | None = None) -> pd.DataFrame:
    """One row per option: id, label, recommended flag and the option ``numbers`` as columns."""
    keys: list[str] = []
    for o in prop.options:
        keys.extend(k for k in o.numbers if k not in keys)
    if max_number_columns is not None:
        keys = keys[:max_number_columns]
    rows = []
    for o in prop.options:
        r: dict[str, Any] = {"option": o.id, "label": o.label, "recommended": o.id == prop.recommended}
        r.update({k: o.numbers.get(k) for k in keys})
        rows.append(r)
    return pd.DataFrame(rows)


def _new_dir(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        d = root / (name if n == 1 else f"{name}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a new snapshot folder for {name}")  # pragma: no cover


def _copy_png(src: Path, dst: Path, dpi: int) -> tuple[int, int]:
    """Write ``src`` (PNG or JPEG) as PNG with ``dpi`` metadata; returns (width, height) in px."""
    with Image.open(src) as im:
        im.load()
        out = im if im.mode in ("RGBA", "LA", "RGB", "L") else im.convert("RGBA")
        out.save(dst, format="PNG", dpi=(dpi, dpi))
        return out.size


def _svg_embedding(png: Path, size: tuple[int, int], dpi: int) -> str:
    """SVG document that shows ``png`` at its print size (``size`` px at ``dpi``)."""
    w, h = size
    b64 = base64.b64encode(png.read_bytes()).decode("ascii")
    wp, hp = w / dpi * 72.0, h / dpi * 72.0
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{wp:.2f}pt" height="{hp:.2f}pt" viewBox="0 0 {w} {h}">'
            f'<image width="{w}" height="{h}" xlink:href="data:image/png;base64,{b64}"/></svg>\n')


def _render_table(cfg: Config, table: pd.DataFrame, title: str, d: Path) -> None:
    """Options table drawn with matplotlib as view.png / view.svg; its cells as source.csv."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..pipeline.plotting import apply_style

    apply_style(cfg.plot)
    shown = table.astype(object).where(pd.notna(table), "")
    if "recommended" in shown:
        shown["recommended"] = ["yes" if v is True else "" for v in shown["recommended"]]
    cells = [[str(v) for v in r] for r in shown.to_numpy()]
    header = [str(c) for c in shown.columns]
    chars = [max(len(x) for x in [h] + [r[j] for r in cells]) for j, h in enumerate(header)]
    fs = cfg.plot.font_size_pt
    width_in = max(3.0, sum(chars) * fs * 0.62 / 72 + 0.15 * len(chars))
    height_in = (len(cells) + 1) * fs * 2.0 / 72 + fs * 2.5 / 72
    fig = plt.figure(figsize=(width_in, height_in))
    ax = fig.add_axes((0, 0, 1, 1 - fs * 2.2 / 72 / height_in))
    ax.axis("off")
    fig.text(0.0, 1.0, title, ha="left", va="top", fontsize=fs + 1, fontweight="bold")
    tab = ax.table(cellText=cells, colLabels=header, colWidths=[c / sum(chars) for c in chars], loc="upper left",
                   cellLoc="left", bbox=(0, 0, 1, 1))
    tab.auto_set_font_size(False)
    tab.set_fontsize(fs)
    for (r, _), cell in tab.get_celld().items():
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_text_props(fontweight="bold")
    fig.patch.set_alpha(0)
    fig.savefig(d / "view.png", dpi=cfg.plot.dpi, transparent=True)
    fig.savefig(d / "view.svg", transparent=True)
    plt.close(fig)
    table.to_csv(d / "source.csv", index=False)


def write_snapshot(
    cfg: Config,
    prop: Proposal,
    sha256: str,
    kind: str,
    option_id: str | None,
    choice: str | None,
    comment: str,
    time_utc: str,
    decision_file: str | None = None,
) -> Path:
    """Create a new snapshot folder for ``prop`` and return its path.

    Parameters
    ----------
    cfg : Config
        Configuration (``plot.dpi``, records folder).
    prop : Proposal
        The proposal.
    sha256 : str
        Hash of the proposal file.
    kind : {"decision", "manual"}
        Written automatically by a decision, or by the Snapshot button.
    option_id : str, optional
        Option whose first figure is shown; otherwise ``prop.main_figure``.
    choice : str, optional
        Decided option id, ``"rejected"`` or ``None`` (undecided).
    comment : str
        Researcher's comment.
    time_utc : str
        ISO time of the event.
    decision_file : str, optional
        Decision file relative to the records folder.
    """
    root = records_dir(cfg)
    stamp = compact(utc_now())
    d = _new_dir(root / "snapshots", f"{stamp}_{prop.stage}_{slugify(prop.title)}")
    try:
        fig_rel = None
        if option_id is not None:
            fig_rel = next(iter(prop.option(option_id).figures), None)
        fig_rel = fig_rel or prop.main_figure
        dpi = int(cfg.plot.dpi)
        table = options_table(prop)
        if fig_rel:
            src = root / "proposals" / fig_rel
            size = _copy_png(src, d / "view.png", dpi)
            if sibling_svg(src).is_file():
                shutil.copyfile(sibling_svg(src), d / "view.svg")
                svg_kind = "copied"
            else:
                (d / "view.svg").write_text(_svg_embedding(d / "view.png", size, dpi), encoding="utf-8")
                svg_kind = "png-embedded"
            if sibling_source(src).is_file():
                shutil.copyfile(sibling_source(src), d / "source.csv")
                source_kind = "figure source table"
            else:
                table.to_csv(d / "source.csv", index=False)
                source_kind = "options table (the figure has no source_<stem>.csv)"
            figure = {"file": fig_rel, "png": "copied", "svg": svg_kind, "source": source_kind}
        else:
            _render_table(cfg, table, prop.title, d)
            figure = {"file": None, "png": "rendered options table", "svg": "rendered options table",
                      "source": "options table"}
        state = {"schema": STATE_SCHEMA, "figure": figure, "option_id": option_id, "dpi": dpi,
                 "settings": prop.figure_state}
        (d / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
        chosen_label = None
        if choice and choice != REJECTED:
            chosen_label = prop.option(choice).label
        note = {"schema": NOTE_SCHEMA, "kind": kind, "stage": prop.stage, "proposal_id": prop.proposal_id,
                "proposal_sha256": sha256, "title": prop.title, "shown_option": option_id, "choice": choice,
                "choice_label": chosen_label, "comment": comment, "time_utc": time_utc, "decision_file": decision_file,
                "proposal": prop.dump(), "morphoagent_version": __version__, "versions": package_versions()}
        (d / "note.json").write_text(json.dumps(note, indent=1), encoding="utf-8")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return d


def thumbnail_data_uri(path: Path, width: int, quality: int = 85) -> str:
    """JPEG data URI of an image at most ``width`` px wide (transparency composited on white)."""
    with Image.open(path) as im:
        im.load()
        rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[3])
    if bg.width > width:
        bg = bg.resize((width, max(1, round(bg.height * width / bg.width))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def list_snapshots(cfg: Config) -> list[dict[str, Any]]:
    """Every snapshot (``note.json`` plus ``dir``), in folder-name order; unreadable folders are skipped."""
    root = records_dir(cfg) / "snapshots"
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        n = d / "note.json"
        if not d.is_dir() or not n.is_file():
            continue
        try:
            note = json.loads(n.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({**note, "dir": str(d), "name": d.name})
    return out
