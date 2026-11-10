"""Figures. Every figure is saved as a 300-dpi transparent PNG, an SVG and a
``source_<name>.csv`` that holds exactly the plotted coordinates."""

from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from .common import LOG  # noqa: E402
from .config import BoxPlotConfig, PlotConfig  # noqa: E402


def apply_style(pc: PlotConfig) -> None:
    """Register extra font files and set rcParams (font, SVG text as text)."""
    for p in pc.font_paths:
        if p and Path(p).is_file():
            font_manager.fontManager.addfont(p)
        else:
            LOG.warning("plot.font_paths: font file not found: %s", p)
    try:
        font_manager.findfont(font_manager.FontProperties(family=pc.font_family), fallback_to_default=False)
    except ValueError:
        LOG.warning(
            "font %r is not available; matplotlib falls back to its default font. "
            "Add the .ttf file to plot.font_paths to use it.", pc.font_family,
        )
    plt.rcParams.update(
        {
            "font.family": pc.font_family,
            "font.size": pc.font_size_pt,
            "svg.fonttype": "none",
            "axes.unicode_minus": False,
        }
    )
    warnings.filterwarnings("ignore", message="findfont: Font family")


def save_figure(fig: plt.Figure, out_dir: Path, name: str, source: pd.DataFrame, dpi: int) -> list[Path]:
    """Save ``<name>.png`` (transparent, ``dpi``), ``<name>.svg`` and ``source_<name>.csv``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.patch.set_alpha(0)
    for ax in fig.axes:
        ax.set_facecolor("none")
    png, svg, csv = out_dir / f"{name}.png", out_dir / f"{name}.svg", out_dir / f"source_{name}.csv"
    fig.savefig(png, dpi=dpi, transparent=True)
    fig.savefig(svg, transparent=True)
    source.to_csv(csv, index=False)
    plt.close(fig)
    return [png, svg, csv]


def _thin_axes(ax: plt.Axes, lw: float) -> None:
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_linewidth(lw)
    ax.tick_params(direction="out", width=lw, length=3)


def stars(value: float, thresholds: list[float]) -> str:
    """Significance stars: '*' per threshold passed (thresholds decreasing), 'ns' otherwise."""
    if value is None or not np.isfinite(value):
        return ""
    n = sum(value < t for t in thresholds)
    return "*" * n if n else "ns"


def _nice_top(vmax: float) -> tuple[float, float]:
    """First 'nice' tick strictly above ``vmax`` and the tick step."""
    loc = matplotlib.ticker.MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10])
    ticks = loc.tick_values(0, vmax if vmax > 0 else 1)
    step = float(ticks[1] - ticks[0])
    return (np.floor(vmax / step) + 1) * step, step


def box_jitter(
    data: dict[str, np.ndarray],
    colors: dict[str, str],
    ylabel: str,
    signif: dict[str, str],
    bc: BoxPlotConfig,
) -> tuple[plt.Figure, pd.DataFrame]:
    """Box (Q1-Q3, bold median) + bold mean 'x' + black jitter dot for every object.

    Parameters
    ----------
    data : dict
        Condition -> 1-D array of object values, in plotting order.
    colors : dict
        Condition -> box fill colour.
    ylabel : str
        Y-axis label (include the unit).
    signif : dict
        Condition -> star string printed above the box ('' for the reference).
    bc : BoxPlotConfig
        Style (line widths in pt, widths in inches, jitter seed).

    Returns
    -------
    (Figure, DataFrame)
        The figure and its source table (one ``box_stats`` row per condition and
        one ``object_dot`` row per plotted dot, with the jittered x).
    """
    conds = list(data)
    rng = np.random.default_rng(bc.jitter_seed)
    whisk_hi, whisk_lo, rows = [], [], []
    for c in conds:
        v = data[c]
        q1, q3 = np.percentile(v, [25, 75])
        iqr = q3 - q1
        whisk_hi.append(v[v <= q3 + bc.whisker_iqr * iqr].max())
        whisk_lo.append(v[v >= q1 - bc.whisker_iqr * iqr].min())
    allv = np.concatenate(list(data.values()))
    vmax = max(whisk_hi) if bc.y_top == "whisker" else float(allv.max())
    ytop, step = _nice_top(vmax)
    ybot = 0.0 if allv.min() >= 0 else (np.floor(allv.min() / step)) * step
    w_in = bc.width_per_box_in * len(conds)
    left, right, bottom, top = 0.95, 0.25, 1.05, 0.45
    fig = plt.figure(figsize=(left + w_in + right, bottom + bc.axis_height_in + top))
    fw, fh = fig.get_figwidth(), fig.get_figheight()
    ax = fig.add_axes([left / fw, bottom / fh, w_in / fw, bc.axis_height_in / fh])
    lw, mlw = bc.line_width_pt, bc.median_line_width_pt
    bp = ax.boxplot(
        [data[c] for c in conds], positions=range(len(conds)), widths=bc.box_width, whis=bc.whisker_iqr,
        patch_artist=True, showfliers=False, medianprops={"color": "k", "lw": mlw},
        boxprops={"lw": lw}, whiskerprops={"lw": lw}, capprops={"lw": lw},
    )
    for patch, c in zip(bp["boxes"], conds, strict=True):
        patch.set_facecolor(colors[c])
        patch.set_edgecolor("k")
        patch.set_linewidth(lw)
    for i, c in enumerate(conds):
        v = data[c]
        x = i + rng.uniform(-bc.jitter_halfwidth, bc.jitter_halfwidth, len(v))
        ax.scatter(x, v, s=bc.dot_size, c="k", alpha=bc.dot_alpha, lw=0, zorder=3)
        ax.plot([i], [v.mean()], marker="x", ms=bc.mean_marker_size, mew=mlw, color="k", zorder=5, ls="none")
        q1, med, q3 = np.percentile(v, 25), np.median(v), np.percentile(v, 75)
        if signif.get(c):
            ax.text(i, ytop, signif[c], ha="center", va="bottom", clip_on=False)
        rows.append(
            {
                "row_type": "box_stats", "condition": c, "x": i, "n": len(v), "mean": v.mean(), "median": med,
                "q1": q1, "q3": q3, "whisker_low": whisk_lo[i], "whisker_high": whisk_hi[i],
                "stars": signif.get(c, ""), "n_above_axis_top": int((v > ytop).sum()), "fill": colors[c],
                "y_axis_bottom": ybot, "y_axis_top": ytop,
            }
        )
        rows += [{"row_type": "object_dot", "condition": c, "x": xx, "value": vv} for xx, vv in zip(x, v, strict=True)]
    ax.set_xticks(range(len(conds)))
    ax.set_xticklabels([f"{c}\n(n={len(data[c])})" for c in conds], rotation=45, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_ylim(ybot, ytop)
    ax.set_xlim(-0.6, len(conds) - 0.4)
    _thin_axes(ax, lw)
    return fig, pd.DataFrame(rows)


def stacked_fraction_bars(
    frac: pd.DataFrame, n: pd.Series, mode_colors: list[str], line_width: float
) -> tuple[plt.Figure, pd.DataFrame]:
    """Stacked bars of shape-mode fractions per condition (rows = conditions, columns = modes)."""
    conds, modes = list(frac.index), list(frac.columns)
    fig, ax = plt.subplots(figsize=(0.95 + 0.45 * len(conds) + 1.3, 3.2))
    bottom = np.zeros(len(conds))
    for j, m in enumerate(modes):
        ax.bar(range(len(conds)), frac[m].to_numpy(), bottom=bottom, width=0.7,
               color=mode_colors[j % len(mode_colors)], edgecolor="k", lw=line_width, label=f"mode {m}")
        bottom += frac[m].to_numpy()
    ax.set_xticks(range(len(conds)))
    ax.set_xticklabels([f"{c}\n(n={int(n[c])})" for c in conds], rotation=45, ha="right")
    ax.set_ylabel("Fraction of objects")
    ax.set_ylim(0, 1)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1], frameon=False, bbox_to_anchor=(1.02, 1), loc="upper left")
    _thin_axes(ax, line_width)
    fig.tight_layout()
    src = frac.copy()
    src.columns = [f"mode_{m}_fraction" for m in modes]
    src.insert(0, "n_objects", [int(n[c]) for c in conds])
    src.insert(0, "x", range(len(conds)))
    src.index.name = "condition"
    return fig, src.reset_index()


def per_mode_fraction_bars(
    table: pd.DataFrame, conds: list[str], colors: dict[str, str], line_width: float, sig_col: str,
    thresholds: list[float],
) -> tuple[plt.Figure, pd.DataFrame]:
    """One panel per shape mode: bar = fraction of objects of each condition in that mode, with stars."""
    modes = sorted(table["mode"].unique())
    fig, axs = plt.subplots(1, len(modes), figsize=(1.2 + 0.4 * len(conds) * len(modes), 3.2), squeeze=False)
    rows = []
    ymax = float(table.fraction.max())
    top = min(1.0, ymax * 1.25 + 0.02)
    for m, ax in zip(modes, axs[0], strict=True):
        t = table[table["mode"] == m].set_index("condition").reindex(conds)
        ax.bar(range(len(conds)), t.fraction, width=0.7, color=[colors[c] for c in conds], edgecolor="k", lw=line_width)
        for i, c in enumerate(conds):
            s = stars(t.loc[c, sig_col], thresholds)
            if s:
                ax.text(i, t.loc[c, "fraction"] + 0.01, s, ha="center", va="bottom")
            rows.append({"mode": m, "condition": c, "x": i, "fraction": t.loc[c, "fraction"],
                         "n_in_mode": int(t.loc[c, "n_in_mode"]), "n_condition": int(t.loc[c, "n_condition"]),
                         sig_col: t.loc[c, sig_col], "stars": s})
        ax.set_title(f"mode {m}")
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels(conds, rotation=45, ha="right")
        ax.set_ylim(0, top)
        _thin_axes(ax, line_width)
    axs[0][0].set_ylabel("Fraction of objects in mode")
    fig.tight_layout()
    return fig, pd.DataFrame(rows)


def elbow_plot(el: pd.DataFrame, k_sd: int, k_kn: int, chosen: list[int], line_width: float,
               n_objects: int) -> tuple[plt.Figure, pd.DataFrame]:
    """Inertia versus K with the second-difference and kneedle suggestions marked."""
    fig, ax = plt.subplots(figsize=(3.8, 2.6))
    ax.plot(el.K, el.inertia, color="k", lw=0.6, marker="o", ms=3, mfc="white", mec="k", mew=0.5,
            label=f"inertia (n={n_objects} objects)")
    ax.axvline(k_sd, color="tab:red", ls="--", lw=0.8, label=f"2nd difference max: K={k_sd}")
    ax.axvline(k_kn, color="tab:blue", ls=":", lw=1.0, label=f"kneedle: K={k_kn}")
    for k in chosen:
        if k in set(el.K):
            ax.plot([k], [float(el.loc[k == el.K, "inertia"].iloc[0])], "o", ms=4.5, color="#F97374", mec="k", mew=0.5)
    ax.set_xlabel("K (number of shape modes)")
    ax.set_ylabel("Inertia (k-means)")
    ax.legend(frameon=False, fontsize=6, bbox_to_anchor=(1.02, 1), loc="upper left")
    _thin_axes(ax, line_width)
    fig.tight_layout()
    src = el.copy()
    src["chosen_k"] = src.K.isin(chosen)
    src["k_second_diff"] = k_sd
    src["k_kneedle"] = k_kn
    return fig, src


def mode_contours(contours: pd.DataFrame, mode_colors: list[str], edge: str, n_by_mode: dict[int, int],
                  line_width: float) -> tuple[plt.Figure, pd.DataFrame]:
    """Filled mean contour of each (display-numbered) shape mode on a common scale."""
    modes = sorted(contours.display_mode.unique())
    xm = float(np.abs(contours.x).max()) * 1.05
    ym = float(np.abs(contours.y).max()) * 1.05
    fig, axs = plt.subplots(1, len(modes), figsize=(2.1 * len(modes), 2.2), squeeze=False)
    for m, ax in zip(modes, axs[0], strict=True):
        g = contours[contours.display_mode == m].sort_values("point")
        x = np.r_[g.x.to_numpy(), g.x.iloc[0]]
        y = np.r_[g.y.to_numpy(), g.y.iloc[0]]
        ax.fill(x, y, facecolor=mode_colors[(m - 1) % len(mode_colors)], edgecolor=edge, lw=line_width)
        ax.set_aspect("equal")
        ax.set_xlim(-xm, xm)
        ax.set_ylim(-ym, ym)
        ax.axis("off")
        ax.set_title(f"mode {m} (n={n_by_mode.get(m, 0)})")
    fig.tight_layout()
    return fig, contours.sort_values(["display_mode", "point"]).reset_index(drop=True)
