"""``morphoagent report``: slide deck and Markdown report from the snapshots.

Snapshots are ordered by stage (:data:`~morphoagent.records.protocol.STAGES`)
and then by time. Each snapshot becomes one 16:9 slide with the figure, a real
table of the options (the chosen row marked), the decision and comment, the
references and a provenance line. The Markdown report has the same content and
links to copies of the figures in ``<stem>_files/``.

File names are ``<prefix>_report_<YYMMDD>_c<N>.pptx`` / ``.md``; ``N`` is one
more than the highest number already present for that prefix (on any date), so
an existing report is never overwritten and the numbers keep increasing across days. ``run_manifest.json`` in the output
folder lists every report built there.

With ``agent.report.redact_paths`` (default) absolute paths shown on slides and
in the Markdown report are shortened (:func:`redact_paths`): paths inside the
configuration folder become relative to it, other paths become
``<path>/<file name>``. ``note.json``, proposals and run manifests keep the full
paths.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from ..pipeline.common import LOG, MANIFEST, MissingInputError, file_sha256, package_versions
from ..pipeline.config import Config
from .protocol import REFERENCE_URLS, REJECTED, STAGE_TITLES, STAGES, Proposal, iso, utc_now
from .snapshots import list_snapshots, options_table

SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5
MIN_COL_IN = 0.55  # no slide-table column is narrower than this
CELL_MARGIN_IN = 0.2  # left + right cell margin
MAX_FONT_PT, MIN_FONT_PT = 10.0, 8.0
TEXT_BOTTOM_IN = 6.85  # the comment box must end above the provenance line
BOLD_FACTOR = 1.12  # bold Arial is about this much wider than regular; table cells are sized for bold text


_POSIX_PATH = re.compile(r"(?<![\w:/.~\\-])/(?:[^\s/\"'|,;()<>\[\]{}]+/)+[^\s/\"'|,;()<>\[\]{}]*")
_WIN_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\s\\/\"'|,;<>]+[\\/])*[^\s\\/\"'|,;<>]*")
REDACTED = "<path>"


def _is_abs_path(text: str) -> bool:
    return bool(re.fullmatch(r"/[^\n]*/[^\n]*|[A-Za-z]:[\\/][^\n]*", text)) and "://" not in text


def _short_path(text: str, base: Path | None) -> str:
    """One absolute path -> config-relative path (inside ``base``) or ``<path>/<name>``."""
    win = bool(re.match(r"[A-Za-z]:[\\/]", text))
    if base is not None and not win:
        try:
            rel = Path(text).resolve(strict=False).relative_to(base.resolve(strict=False))
            return rel.as_posix() or "."
        except ValueError:
            pass
    name = re.split(r"[\\/]", text.rstrip("/\\"))[-1]
    return f"{REDACTED}/{name}" if name else REDACTED


def redact_paths(value: Any, base: str | Path | None = None) -> Any:
    """Shorten absolute paths in a string or in nested lists / dicts (for display only).

    Parameters
    ----------
    value : Any
        String, list, tuple or dict (other values are returned unchanged).
    base : str or Path, optional
        Folder of the configuration file; paths inside it become relative paths.

    Returns
    -------
    Any
        The same structure with every absolute POSIX or Windows path replaced by a
        path relative to ``base`` or by ``<path>/<file name>``. A string that is
        itself one path (it may contain spaces) is replaced as a whole; in free text
        the paths are found with a pattern that stops at white space.
    """
    b = Path(base) if base else None
    if isinstance(value, str):
        if _is_abs_path(value.strip()):
            return _short_path(value.strip(), b)
        out = _POSIX_PATH.sub(lambda m: _short_path(m.group(0), b), value)
        return _WIN_PATH.sub(lambda m: _short_path(m.group(0), b), out)
    if isinstance(value, dict):
        return {k: redact_paths(v, b) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_paths(v, b) for v in value)
    return value


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, float):
        if v != v:  # NaN: the option has no value for this column
            return ""
        return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.3g}"
    return str(v)


def reference_url(ref: str) -> str:
    """Web address of a normalised reference (``PMID:<n>`` or ``doi:<doi>``)."""
    if ref.startswith("PMID:"):
        return REFERENCE_URLS["pmid"] + ref[5:] + "/"
    return REFERENCE_URLS["doi"] + ref[4:]


def ordered_snapshots(cfg: Config) -> list[dict[str, Any]]:
    """Snapshots sorted by stage order, then time, then folder name."""
    snaps = list_snapshots(cfg)
    rank = {s: i for i, s in enumerate(STAGES)}
    return sorted(snaps, key=lambda s: (rank.get(s.get("stage"), len(STAGES)), s.get("time_utc") or "", s["name"]))


def slide_content(note: dict[str, Any], max_rows: int, max_cols: int | None, redact: bool = False,
                  base: str | Path | None = None) -> dict[str, Any]:
    """Everything one slide (or Markdown section) shows, derived from a snapshot's ``note.json``.

    With ``redact`` the displayed texts, table cells and provenance parameters pass
    through :func:`redact_paths` (``base`` = configuration folder).
    """
    prop = Proposal.model_validate(note["proposal"])
    choice = note.get("choice")
    tab = options_table(prop, max_cols)
    marks = []
    for o in prop.options:
        m = []
        if choice == o.id:
            m.append("chosen")
        if prop.recommended == o.id:
            m.append("recommended")
        marks.append(", ".join(m))
    tab.insert(0, "mark", marks)
    tab = tab.drop(columns="recommended")
    n_total = len(tab)
    if n_total > max_rows:  # keep the first rows, and always the chosen one
        idx = list(range(max_rows))
        chosen = [i for i, o in enumerate(prop.options) if o.id == choice]
        if chosen and chosen[0] >= max_rows:
            idx[-1] = chosen[0]
        tab = tab.iloc[idx]
    hidden = n_total - len(tab)
    if choice is None:
        decision = "Not decided at snapshot time"
    elif choice == REJECTED:
        decision = "Rejected (no option chosen)"
    else:
        decision = f"Chosen: {choice} - {prop.option(choice).label}"
    if note.get("kind") == "manual":
        shown = note.get("shown_option")
        decision = f"Manual snapshot{f' of option {shown}' if shown else ''}. {decision}"
    refs: list[str] = []
    for o in prop.options:
        if choice in (None, REJECTED) or o.id == choice:
            refs.extend(o.references)
    for e in prop.evidence:
        refs.extend(e.references)
    refs = list(dict.fromkeys(refs))
    prov = prop.provenance
    shown_params = redact_paths(prov.parameters, base) if redact else prov.parameters
    params = json.dumps(shown_params, separators=(",", ":"), default=str)
    if len(params) > 240:
        params = params[:237] + "..."
    provenance = (f"stage {prop.stage} | tool {prov.tool or '-'} | parameters {params} | proposal {prop.proposal_id} "
                  f"(sha256 {note['proposal_sha256'][:12]}) | snapshot {note['name']} | {note.get('time_utc')}")
    out = {"title": f"{STAGE_TITLES.get(prop.stage, prop.stage)}: {prop.title}", "decision": decision,
           "comment": note.get("comment") or "", "rationale": prop.rationale, "table": tab, "hidden_rows": hidden,
           "references": refs, "provenance": provenance, "image": Path(note["dir"]) / "view.png",
           "table_only": _figure_less(Path(note["dir"]))}
    if redact:
        for key in ("title", "decision", "comment", "rationale", "provenance"):
            out[key] = redact_paths(out[key], base)
        out["table"] = tab.map(lambda v: redact_paths(v, base) if isinstance(v, str) else v)
        out["table"].columns = [redact_paths(str(c), base) for c in tab.columns]
    return out


def _figure_less(snapshot_dir: Path) -> bool:
    """True when the snapshot's view is only a drawing of the options table (the proposal had no figure)."""
    try:
        state = json.loads((snapshot_dir / "state.json").read_text())
    except (OSError, ValueError):
        return False
    return (state.get("figure") or {}).get("file") is None


def _next_stem(out_dir: Path, prefix: str, day: str) -> tuple[str, int]:
    rx = re.compile(rf"^{re.escape(prefix)}_report_\d{{6}}_c(\d+)(?:\.pptx|\.md|_files)$")
    nums = [int(m.group(1)) for p in out_dir.iterdir() if (m := rx.match(p.name))] if out_dir.is_dir() else []
    n = max(nums, default=0) + 1
    return f"{prefix}_report_{day}_c{n}", n


def _text(slide: Any, left: float, top: float, width: float, height: float, paragraphs: list[tuple[str, float, bool]],
          font: str, color: tuple[int, int, int] = (0, 0, 0)) -> Any:
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    for i, (txt, size, bold) in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = txt
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = font
        r.font.color.rgb = RGBColor(*color)
    return box


def _wrap_lines(text: str, width_in: float, char_in: float) -> int:
    """Estimated number of lines of ``text`` wrapped at spaces into ``width_in`` inches."""
    n, cur = 1, 0.0
    cap = max(width_in, char_in)
    for word in text.split():
        w = len(word) * char_in
        if cur and cur + char_in + w > cap:
            n, cur = n + 1, w
        else:
            cur = cur + char_in + w if cur else w
        while cur > cap:  # an unbreakable word wider than the cell is broken by the renderer
            n, cur = n + 1, cur - cap
    return n


def _display_header(name: Any) -> str:
    """Header text on the slide: underscores become spaces so a long name wraps between words."""
    return str(name).replace("_", " ")


def table_geometry(tab: Any, width_in: float, font_pt: float = MAX_FONT_PT) -> tuple[list[float], list[float]]:
    """Column widths from the content and row heights estimated from wrapping.

    Each column gets at least the width of its longest unbreakable word (and :data:`MIN_COL_IN`);
    the remaining width is shared in proportion to how much more each column would like to have.
    The widths always add up to ``width_in`` unless the minimum widths alone exceed it.

    Parameters
    ----------
    tab : pandas.DataFrame
        Table as shown (first row of the slide is the header).
    width_in : float
        Available width in inches.
    font_pt : float
        Font size in points.

    Returns
    -------
    tuple of list of float
        Column widths and row heights in inches (header row first).
    """
    rows = [[_display_header(c) for c in tab.columns]] + [[_fmt(v) for v in r] for r in tab.itertuples(index=False)]
    char_in = font_pt * 0.6 / 72 * BOLD_FACTOR  # header and chosen rows are bold: size every cell for bold text
    line_in = font_pt * 1.25 / 72
    ncol = len(rows[0])
    word = [max((len(w) for r in rows for w in r[j].split()), default=1) * char_in + CELL_MARGIN_IN for j in range(ncol)]
    want = [max(len(r[j]) for r in rows) * char_in + CELL_MARGIN_IN for j in range(ncol)]
    lo = [max(MIN_COL_IN, w) for w in word]
    hi = [max(a, b) for a, b in zip(lo, want, strict=True)]
    spare = width_in - sum(lo)
    if spare <= 0:
        widths = lo
    else:
        grow = [h - m for h, m in zip(hi, lo, strict=True)]
        total = sum(grow)
        if total <= spare:  # everything fits at its wanted width: spread the rest in proportion
            extra = (spare - total) / sum(hi)
            widths = [h + extra * h for h in hi]
        else:
            widths = [m + spare * g / total for m, g in zip(lo, grow, strict=True)]
    heights = []
    for r in rows:
        lines = max(_wrap_lines(t, w - CELL_MARGIN_IN, char_in) for t, w in zip(r, widths, strict=True))
        heights.append(0.1 + lines * line_in)
    return widths, heights


def fit_table(tab: Any, width_in: float, max_height_in: float, max_numeric_cols: int) -> dict[str, Any]:
    """Choose the columns, font size and geometry of the slide table.

    The first three columns (``mark``, ``option``, ``label``) are always kept. At most
    ``max_numeric_cols`` of the remaining (numeric) columns are shown; more are dropped until the
    minimum column widths fit at 8 pt, and the font shrinks from 10 pt to 8 pt (0.5 pt steps) until
    the estimated height is at most ``max_height_in``.

    Returns
    -------
    dict
        ``table`` (shown columns), ``font_pt``, ``widths``, ``heights`` and ``dropped`` (number of numeric
        columns left to the Markdown report).
    """
    fixed = list(tab.columns[:3])
    numeric = list(tab.columns[3:])
    k = min(len(numeric), max(max_numeric_cols, 0))
    while True:
        shown = tab[fixed + numeric[:k]]
        pt = MAX_FONT_PT
        while True:
            widths, heights = table_geometry(shown, width_in, pt)
            fits_w = sum(widths) <= width_in + 1e-6
            if (fits_w and sum(heights) <= max_height_in) or pt <= MIN_FONT_PT:
                break
            pt -= 0.5
        if (fits_w and sum(heights) <= max_height_in) or k == 0:
            break
        k -= 1
    return {"table": shown, "font_pt": pt, "widths": widths, "heights": heights, "dropped": len(numeric) - k}


def fit_text(paras: list[tuple[str, float, bool]], width_in: float, avail_in: float) -> list[tuple[str, float, bool]]:
    """Make paragraphs fit into ``avail_in`` inches of height.

    The font sizes shrink together (never below 8 pt); if the text is still too tall the longest paragraph
    is cut and ends with an ellipsis. The caller keeps the full text elsewhere (slide notes).
    """
    def height(ps: list[tuple[str, float, bool]]) -> float:
        h = 0.1
        for t, size, _ in ps:
            h += _wrap_lines(t, width_in - CELL_MARGIN_IN, size * 0.55 / 72) * size * 1.2 / 72
        return h

    scale = 1.0
    cur = list(paras)
    while height(cur) > avail_in and scale > 0.0:
        scale -= 0.05
        cur = [(t, max(MIN_FONT_PT, round(sz * scale * 2) / 2), b) for t, sz, b in paras]
        if all(sz <= MIN_FONT_PT for _, sz, _ in cur):
            break
    while height(cur) > avail_in:
        i = max(range(len(cur)), key=lambda k: len(cur[k][0]))
        t, sz, b = cur[i]
        if len(t) <= 12:
            break
        cur[i] = (t[:max(12, int(len(t) * 0.85)) - 3].rstrip() + "...", sz, b)
    return cur


def _slide(prs: Any, c: dict[str, Any], font: str, max_numeric_cols: int = 6) -> dict[str, float]:
    """Add one slide; returns the table and comment-box geometry in inches (``table_top``, ``table_h``, ``box_top``)."""
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    s = prs.slides.add_slide(prs.slide_layouts[6])
    _text(s, 0.4, 0.25, 12.5, 0.6, [(c["title"], 22, True)], font)
    _text(s, 0.4, 0.85, 12.5, 0.4, [(c["decision"], 14, True)], font, (0, 70, 140))
    if c.get("table_only"):
        # no figure: the snapshot view is only a drawing of this table, so the real table takes the full width
        left, top, width = 0.4, 1.35, 12.5
    else:
        # figure, fitted into the left box
        box_l, box_t, box_w, box_h = 0.4, 1.35, 6.3, 5.45
        with Image.open(c["image"]) as im:
            w, h = im.size
        scale = min(box_w / w, box_h / h)
        s.shapes.add_picture(str(c["image"]), Inches(box_l + (box_w - w * scale) / 2), Inches(box_t),
                             Inches(w * scale), Inches(h * scale))
        left, top, width = 6.95, 1.35, 6.0
    # options table: at most 60 % of the column height, the rest is for the comment box
    fit = fit_table(c["table"], width, 0.6 * (TEXT_BOTTOM_IN - top), max_numeric_cols)
    tab, widths, heights, pt = fit["table"], fit["widths"], fit["heights"], fit["font_pt"]
    n_rows, n_cols = len(tab) + 1, len(tab.columns)
    table_h = sum(heights)
    gt = s.shapes.add_table(n_rows, n_cols, Inches(left), Inches(top), Inches(sum(widths)), Inches(table_h)).table
    for j, w in enumerate(widths):
        gt.columns[j].width = Inches(w)
    for i, h in enumerate(heights):
        gt.rows[i].height = Inches(h)
    chosen_rows = [i for i, m in enumerate(tab["mark"]) if "chosen" in str(m)]
    for j, col in enumerate(tab.columns):
        cells = [_display_header(col)] + [_fmt(v) for v in tab[col]]
        for i, txt in enumerate(cells):
            cell = gt.cell(i, j)
            cell.text = txt
            cell.margin_left = cell.margin_right = Inches(CELL_MARGIN_IN / 2)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(pt)
                    r.font.name = font
                    r.font.bold = i == 0 or (i - 1) in chosen_rows
            if i >= 1 and (i - 1) in chosen_rows:
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor(0xD9, 0xEA, 0xF7)
    # comment box strictly below the table (its real height) plus a gap
    y = top + table_h + 0.2
    notes: list[str] = []
    paras: list[tuple[str, float, bool]] = []
    if c["hidden_rows"]:
        paras.append((f"{c['hidden_rows']} more option(s) not shown", 10, False))
    if fit["dropped"]:
        paras.append((f"{fit['dropped']} more column(s) in the Markdown report only", 10, False))
    paras.append((f"Comment: {c['comment'] or '-'}", 12, False))
    if c["rationale"]:
        paras.append((f"Agent rationale: {c['rationale']}", 10, False))
    refs = "; ".join(c["references"])
    ref_line = f"References: {refs}" if refs else ""
    full = [p[0] for p in paras] + ([ref_line] if ref_line else [])
    fitted = fit_text(paras + ([(ref_line, 10, False)] if ref_line else []), width, TEXT_BOTTOM_IN - y)
    shown_refs = fitted[-1] if ref_line else None
    body = fitted[:-1] if ref_line else fitted
    if [p[0] for p in fitted] != full:
        notes = full
    tb = _text(s, left, y, width, max(0.3, TEXT_BOTTOM_IN - y), body, font)
    if ref_line and shown_refs is not None:
        p = tb.text_frame.add_paragraph()
        r0 = p.add_run()
        r0.text = "References: "
        r0.font.size, r0.font.name = Pt(shown_refs[1]), font
        if shown_refs[0] == ref_line:
            for k, ref in enumerate(c["references"]):
                r = p.add_run()
                r.text = ref + ("; " if k < len(c["references"]) - 1 else "")
                r.font.size, r.font.name = Pt(shown_refs[1]), font
                r.hyperlink.address = reference_url(ref)
        else:
            r = p.add_run()
            r.text = shown_refs[0][len("References: "):]
            r.font.size, r.font.name = Pt(shown_refs[1]), font
    _text(s, 0.4, 6.95, 12.5, 0.4, [(c["provenance"], 8, False)], font, (110, 110, 110))
    s.notes_slide.notes_text_frame.text = "\n\n".join(notes) if notes else "\n\n".join(full)
    return {"table_top": top, "table_h": table_h, "box_top": y}


def _markdown(contents: list[dict[str, Any]], images: list[str], title: str) -> str:
    lines = [f"# {title}", ""]
    for c, img in zip(contents, images, strict=True):
        lines += [f"## {c['title']}", "", f"**{c['decision']}**", "", f"![figure]({img})", ""]
        tab = c["table"]
        lines.append("| " + " | ".join(str(x) for x in tab.columns) + " |")
        lines.append("|" + "---|" * len(tab.columns))
        for r in tab.itertuples(index=False):
            lines.append("| " + " | ".join(_fmt(v).replace("|", "\\|") for v in r) + " |")
        lines.append("")
        if c["hidden_rows"]:
            lines += [f"{c['hidden_rows']} more option(s) not shown.", ""]
        lines += [f"Comment: {c['comment'] or '-'}", ""]
        if c["rationale"]:
            lines += [f"Agent rationale: {c['rationale']}", ""]
        if c["references"]:
            lines += ["References: " + "; ".join(f"[{r}]({reference_url(r)})" for r in c["references"]), ""]
        lines += [f"<sub>{c['provenance']}</sub>", ""]
    return "\n".join(lines)


def build_report(cfg: Config, out_dir: str | Path, prefix: str | None = None) -> dict[str, Any]:
    """Build ``<prefix>_report_<YYMMDD>_c<N>.pptx`` and ``.md`` from every snapshot.

    Parameters
    ----------
    cfg : Config
        Configuration (records folder, ``agent.report`` settings).
    out_dir : str or Path
        Output folder (created when missing).
    prefix : str, optional
        File-name prefix (default ``agent.report.prefix``).

    Returns
    -------
    dict
        ``pptx``, ``markdown``, ``files_dir``, ``n_slides``, ``snapshots``
        (folder names in slide order) and ``manifest``.
    """
    from pptx import Presentation
    from pptx.util import Inches

    rc = cfg.agent.report
    prefix = prefix or rc.prefix
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", prefix):
        raise ValueError("prefix must be 1-60 characters: letters, digits, '_', '.', '-'")
    snaps = ordered_snapshots(cfg)
    if not snaps:
        raise MissingInputError("no snapshots yet: decide a proposal or press Snapshot on a decision card first")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = _dt.datetime.now().strftime("%y%m%d")
    stem, n = _next_stem(out, prefix, day)
    pptx_path, md_path, files_dir = out / f"{stem}.pptx", out / f"{stem}.md", out / f"{stem}_files"
    files_dir.mkdir()  # fails if it exists: never overwrite
    try:
        base = Path(cfg.config_path).parent if cfg.config_path else None
        contents = [slide_content(s, rc.max_table_rows, rc.max_number_columns, rc.redact_paths, base) for s in snaps]
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(SLIDE_W_IN), Inches(SLIDE_H_IN)
        images = []
        for i, c in enumerate(contents, start=1):
            _slide(prs, c, rc.font_family, rc.max_table_columns)
            img = files_dir / f"{i:02d}_{snaps[i - 1]['name']}.png"
            shutil.copyfile(c["image"], img)
            images.append(f"{files_dir.name}/{img.name}")
        with open(pptx_path, "xb") as fh:
            prs.save(fh)
        with open(md_path, "x", encoding="utf-8") as fh:
            fh.write(_markdown(contents, images, f"{prefix} report ({len(contents)} snapshot(s))"))
    except BaseException:
        shutil.rmtree(files_dir, ignore_errors=True)
        raise
    from ..pipeline.ui.llm import atomic_write_json

    mpath = out / MANIFEST
    try:
        m = json.loads(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    except (OSError, ValueError):
        m = {}
    m.update({"tool": "report", "timestamp_utc": iso(utc_now()), "versions": package_versions(),
              "config_file": cfg.config_path, "config_sha256": file_sha256(cfg.config_path),
              "parameters": {"prefix": prefix, "max_table_rows": rc.max_table_rows,
                             "max_number_columns": rc.max_number_columns,
                             "max_table_columns": rc.max_table_columns, "font_family": rc.font_family,
                             "redact_paths": rc.redact_paths}})
    m.setdefault("reports", []).append({
        "pptx": pptx_path.name, "markdown": md_path.name, "files_dir": files_dir.name, "number": n,
        "n_slides": len(contents), "pptx_sha256": file_sha256(pptx_path), "timestamp_utc": iso(utc_now()),
        "snapshots": [s["name"] for s in snaps]})
    m["counts_out"] = {"reports": len(m["reports"]), "slides_last": len(contents)}
    atomic_write_json(mpath, m)
    LOG.info("report: %s (%d slide(s)) and %s", pptx_path, len(contents), md_path.name)
    return {"pptx": str(pptx_path), "markdown": str(md_path), "files_dir": str(files_dir), "n_slides": len(contents),
            "snapshots": [s["name"] for s in snaps], "manifest": str(mpath)}
