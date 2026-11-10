"""Numbered outline overlays for mask review (step 7) and outline montages (step 4)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from skimage.morphology import dilation, disk
from skimage.segmentation import find_boundaries

from .common import get_font
from .config import ReviewConfig


def outline(labels: np.ndarray, width_px: int) -> np.ndarray:
    """Boolean outline mask: inner boundary of every label, dilated to ``width_px``."""
    b = find_boundaries(labels, mode="inner")
    return dilation(b, disk(max(int(width_px) - 1, 1)))


def inside_point(mask: np.ndarray) -> tuple[int, int]:
    """(x, y) of the deepest point inside a mask (maximum of the distance transform)."""
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    dt = ndi.distance_transform_edt(np.pad(mask[y0:y1, x0:x1], 1))[1:-1, 1:-1]
    iy, ix = np.unravel_index(np.argmax(dt), dt.shape)
    return int(x0 + ix), int(y0 + iy)


def numbered_overlay(
    background: np.ndarray,
    labels: np.ndarray,
    kept: Iterable[int],
    frame: Iterable[int],
    areas: dict[int, float],
    rc: ReviewConfig,
) -> np.ndarray:
    """Draw kept (solid) and frame-contact (dashed) outlines with label numbers.

    Parameters
    ----------
    background : numpy.ndarray
        uint8 grey image with the same shape as ``labels``.
    labels : numpy.ndarray
        Label image.
    kept, frame : iterable of int
        Labels drawn as kept (solid, ``kept_linewidth_px``) and as
        frame-contact (dashed, ``frame_contact_linewidth_px``). Other labels are
        not drawn.
    areas : dict
        Label -> area in px^2; the font size is ``clip(font_scale * sqrt(area),
        font_min_px, font_max_px)``.
    rc : ReviewConfig
        Colours, line widths and font settings.

    Returns
    -------
    numpy.ndarray
        RGB uint8 image.
    """
    kept, frame = np.asarray(list(kept), int), np.asarray(list(frame), int)
    img = np.stack([background] * 3, -1).astype(np.uint8)
    yy, xx = np.indices(labels.shape)
    dash = ((xx // rc.dash_px) + (yy // rc.dash_px)) % 2 == 0
    img[outline(np.where(np.isin(labels, frame), labels, 0), rc.frame_contact_linewidth_px) & dash] = rc.frame_contact_color
    img[outline(np.where(np.isin(labels, kept), labels, 0), rc.kept_linewidth_px)] = rc.kept_color
    pil = Image.fromarray(img)
    dr = ImageDraw.Draw(pil)
    fonts: dict[int, object] = {}
    for lb in list(kept) + list(frame):
        m = labels == lb
        if not m.any():
            continue
        x, y = inside_point(m)
        fs = int(np.clip(rc.font_scale * np.sqrt(float(areas[int(lb)])), rc.font_min_px, rc.font_max_px))
        if fs not in fonts:
            fonts[fs] = get_font(rc.font_path, fs)
        dr.text((x, y), str(int(lb)), fill=tuple(rc.text_color), font=fonts[fs], anchor="mm",
                stroke_width=max(rc.stroke_min_px, fs // rc.stroke_divisor), stroke_fill=tuple(rc.stroke_color))
    return np.array(pil)


def outline_panel(gray: np.ndarray, labels: np.ndarray, width: int, color: list[int]) -> np.ndarray:
    """Small RGB panel: grey image and labels resized to ``width`` px (aspect of the labels), 1-px outlines."""
    h, w = labels.shape
    height = int(round(h * width / w))
    bg = np.array(Image.fromarray(gray).resize((width, height), Image.Resampling.LANCZOS))
    lab = np.array(Image.fromarray(labels.astype(np.int32)).resize((width, height), Image.Resampling.NEAREST))
    rgb = np.stack([bg] * 3, -1)
    rgb[find_boundaries(lab, mode="inner")] = color
    return rgb


def grid_sheet(panels: list[list[np.ndarray]], captions: list[list[str]], col_titles: list[str] | None,
               font_path: str | None, caption_px: int = 18, pad: int = 6) -> np.ndarray:
    """Assemble RGB panels (rows x cols, equal sizes per row) into one sheet with captions."""
    font = get_font(font_path, caption_px)
    ph = max(p.shape[0] for row in panels for p in row)
    pw = max(p.shape[1] for row in panels for p in row)
    ncol = max(len(r) for r in panels)
    head = (caption_px + 2 * pad) if col_titles else 0
    cell_h = ph + caption_px * 2 + 3 * pad
    sheet = Image.new("RGB", (ncol * (pw + pad) + pad, head + len(panels) * cell_h + pad), "white")
    dr = ImageDraw.Draw(sheet)
    if col_titles:
        for j, t in enumerate(col_titles):
            dr.text((pad + j * (pw + pad), pad), t, fill="black", font=font)
    for i, row in enumerate(panels):
        for j, p in enumerate(row):
            x0, y0 = pad + j * (pw + pad), head + i * cell_h + pad
            sheet.paste(Image.fromarray(p if p.ndim == 3 else np.stack([p] * 3, -1)), (x0, y0))
            for k, line in enumerate(captions[i][j].split("\n")[:2]):
                dr.text((x0, y0 + ph + pad + k * caption_px), line, fill="black", font=font)
    return np.array(sheet)


def _ellipsize(text: str, font: Any, width: float) -> str:
    """Shorten ``text`` with a trailing ellipsis until it is at most ``width`` px wide."""
    if font.getlength(text) <= width:
        return text
    while len(text) > 1 and font.getlength(text + "...") > width:
        text = text[:-1]
    return text + "..."


def wrap_to_width(text: str, font: Any, width: float, max_lines: int) -> list[str]:
    """Wrap ``text`` at spaces into at most ``max_lines`` lines of at most ``width`` px.

    Words wider than ``width`` are cut; the last line is ellipsized when text remains.
    """
    tokens = re.findall(r"[^ ]+|[ ]", text.replace("\n", " "))
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        if font.getlength((cur + tok).rstrip()) <= width or not cur:
            cur += tok
        else:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
    if cur.strip():
        lines.append(cur.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [_ellipsize(x, font, width) for x in lines]


def contact_sheet(panels: list[list[np.ndarray]], captions: list[list[list[str]]], col_titles: list[str],
                  font_path: str | None, text_px: int = 18, min_text_px: int = 11, pad: int = 8,
                  title_lines: int = 2, caption_lines: int = 3,
                  draw_text: bool = True) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Panel grid with column titles and per-panel captions that are wrapped to the panel width.

    The text size shrinks (down to ``min_text_px``) until the longest unbreakable word fits, then
    titles and captions are wrapped, and the title band and caption rows are sized to the wrapped text,
    so text never reaches a neighbouring panel.

    Parameters
    ----------
    panels : list of list of numpy.ndarray
        RGB panels ``[row][col]``.
    captions : list of list of list of str
        Caption lines per panel (``captions[row][col]`` is a list of short lines, e.g. name, metrics).
    col_titles : list of str
        One title per column.
    font_path : str, optional
        TrueType font; a default is used when ``None``.
    text_px : int
        Preferred text height in pixels.
    min_text_px : int
        Smallest text height used before text is cut with an ellipsis.
    pad : int
        Gap between cells in pixels.
    title_lines, caption_lines : int
        Maximum wrapped lines for a title and for a whole caption.
    draw_text : bool
        Draw the text into the raster (``False`` gives the text-free raster for the SVG layer).

    Returns
    -------
    tuple
        RGBA image (transparent background) and a list of text items ``{x, y, size, text}``
        (pixel positions of the top-left of each line) for vector export.
    """
    pw = max(p.shape[1] for row in panels for p in row)
    ph = max(p.shape[0] for row in panels for p in row)
    words = [w for t in col_titles for w in t.split()] + [w for r in captions for c in r for ln in c
                                                                      for w in ln.split()]
    size = text_px
    while size > min_text_px and any(get_font(font_path, size).getlength(w) > pw for w in words):
        size -= 1
    font = get_font(font_path, size)
    line_h = int(size * 1.25)
    titles = [wrap_to_width(t, font, pw, title_lines) for t in col_titles]
    caps = [[[x for ln in c for x in wrap_to_width(ln, font, pw, 2)][:caption_lines] for c in r] for r in captions]
    head = max(len(t) for t in titles) * line_h + 2 * pad
    row_h = [ph + pad + max(len(c) for c in r) * line_h + 2 * pad for r in caps]
    ncol = max(len(r) for r in panels)
    img = Image.new("RGBA", (ncol * (pw + pad) + pad, head + sum(row_h) + pad), (255, 255, 255, 0))
    dr = ImageDraw.Draw(img)
    items: list[dict[str, Any]] = []

    def put(x: int, y: int, t: str) -> None:
        if draw_text:
            dr.text((x, y), t, fill=(0, 0, 0, 255), font=font)
        items.append({"x": x, "y": y, "size": size, "text": t})

    for j, t in enumerate(titles):
        for k, ln in enumerate(t):
            put(pad + j * (pw + pad), pad + k * line_h, ln)
    y0 = head
    for i, row in enumerate(panels):
        for j, p in enumerate(row):
            x0 = pad + j * (pw + pad)
            rgb = p if p.ndim == 3 else np.stack([p] * 3, -1)
            img.paste(Image.fromarray(rgb).convert("RGBA"), (x0, y0))
            for k, ln in enumerate(caps[i][j]):
                put(x0, y0 + ph + pad + k * line_h, ln)
        y0 += row_h[i]
    return img, items


def sheet_svg(img: Image.Image, items: list[dict[str, Any]]) -> str:
    """SVG of a contact sheet: the raster panels embedded as one PNG, text kept as live ``<text>`` elements.

    ``img`` should be the text-free raster (``contact_sheet(..., draw_text=False)``).
    """
    import base64
    import html
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    w, h = img.size
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<image width="{w}" height="{h}" href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>']
    for t in items:
        parts.append(f'<text x="{t["x"]}" y="{t["y"] + t["size"]}" font-family="Arial, sans-serif" '
                     f'font-size="{t["size"]}" fill="black">{html.escape(t["text"])}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
