"""Step 10: object-level statistics and plots.

Every test treats individual objects (cells) as observations. No per-image
aggregation is performed.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, ttest_ind

from .common import (
    LOG,
    MANIFEST,
    RunContext,
    analysis_set,
    condition_order,
    prepare_step_dir,
    reference_condition,
    step_dir,
    write_manifest,
)
from .config import Config, pixel_size_um
from .plotting import apply_style, box_jitter, per_mode_fraction_bars, save_figure, stacked_fraction_bars, stars

AXIS_LABELS = {
    "area_px": "Area (px²)", "area_um2": "Area (µm²)", "perimeter_px": "Perimeter (px)",
    "aspect_ratio": "Aspect ratio (major / minor axis)", "form_factor": "Form factor (4πA/P²)",
    "solidity": "Solidity", "eccentricity": "Eccentricity", "extent": "Extent",
    "major_axis_length": "Major axis length (px)", "minor_axis_length": "Minor axis length (px)",
}


def bh(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (NaN entries are ignored and stay NaN)."""
    p = np.asarray(p, float)
    out = np.full(len(p), np.nan)
    ok = np.isfinite(p)
    if not ok.any():
        return out
    q = p[ok]
    n = len(q)
    o = np.argsort(q)
    adj = q[o] * n / (np.arange(n) + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    r = np.empty(n)
    r[o] = np.minimum(adj, 1)
    out[ok] = r
    return out


def summarise(cells: pd.DataFrame, metric: str, conds: list[str], ref: str) -> pd.DataFrame:
    """Per-condition n / mean / median / quartiles, ratio to reference, Welch p and BH q."""
    r = cells.loc[cells.condition == ref, metric].dropna().to_numpy(float)
    rows = []
    for c in conds:
        v = cells.loc[cells.condition == c, metric].dropna().to_numpy(float)
        p = np.nan if c == ref or len(v) < 2 or len(r) < 2 else float(ttest_ind(v, r, equal_var=False).pvalue)
        rows.append({"metric": metric, "condition": c, "n": len(v), "mean": v.mean() if len(v) else np.nan,
                     "sd": v.std(ddof=1) if len(v) > 1 else np.nan, "median": np.median(v) if len(v) else np.nan,
                     "q1": np.percentile(v, 25) if len(v) else np.nan, "q3": np.percentile(v, 75) if len(v) else np.nan,
                     "ratio_mean_vs_reference": (v.mean() / r.mean()) if len(v) and len(r) else np.nan,
                     "ratio_median_vs_reference": (np.median(v) / np.median(r)) if len(v) and len(r) else np.nan,
                     "p_welch_vs_reference": p})
    s = pd.DataFrame(rows)
    s["q_bh"] = bh(s.p_welch_vs_reference.to_numpy())
    s.insert(2, "reference", ref)
    return s


def composition(modes: pd.DataFrame, conds: list[str], ref: str, k: int) -> pd.DataFrame:
    """Mode counts / fractions per condition with Fisher exact tests (mode vs rest, vs reference) + BH per mode."""
    cnt = pd.crosstab(modes.condition, modes.shape_mode).reindex(index=conds, columns=range(1, k + 1), fill_value=0)
    n = cnt.sum(1)
    parts = []
    for m in range(1, k + 1):
        a0, n0 = int(cnt.loc[ref, m]), int(n[ref])
        rows = []
        for c in conds:
            a, nc = int(cnt.loc[c, m]), int(n[c])
            if c == ref or nc == 0:
                orr, p = np.nan, np.nan
            else:
                orr, p = fisher_exact([[a, nc - a], [a0, n0 - a0]])
            rows.append({"K": k, "mode": m, "condition": c, "reference": ref, "n_in_mode": a, "n_condition": nc,
                         "fraction": a / nc if nc else np.nan,
                         "ratio_vs_reference": (a / nc) / (a0 / n0) if nc and a0 else np.nan,
                         "odds_ratio": orr, "p_fisher": p})
        t = pd.DataFrame(rows)
        t["q_bh"] = bh(t.p_fisher.to_numpy())
        parts.append(t)
    return pd.concat(parts, ignore_index=True)


def shannon(modes: pd.Series, k: int, n_boot: int, rng: np.random.Generator, ci: float) -> tuple[float, float, float]:
    """Shannon entropy (natural log) of mode fractions with a percentile bootstrap CI over objects."""
    v = modes.to_numpy(int)

    def h(x: np.ndarray) -> float:
        p = np.bincount(x, minlength=k + 1)[1:] / len(x)
        p = p[p > 0]
        return float(-(p * np.log(p)).sum())

    boots = np.array([h(rng.choice(v, len(v), replace=True)) for _ in range(n_boot)])
    lo, hi = np.percentile(boots, [(100 - ci) / 2, 100 - (100 - ci) / 2])
    return h(v), float(lo), float(hi)


def run_stats(cfg: Config, ctx: RunContext) -> None:
    """Object-level summaries, Welch tests, mode composition tests and plots."""
    st = cfg.stats
    _, cells, _, from_step = analysis_set(cfg)
    conds = condition_order(cfg, cells.condition)
    ref = reference_condition(cfg, conds)
    cells = cells.copy()
    um_per_px = pixel_size_um(cfg)
    if um_per_px:
        cells["area_um2"] = cells.area_px * um_per_px ** 2
    metrics = [m for m in st.metrics if m in cells] + (["area_um2"] if um_per_px else [])
    missing = [m for m in st.metrics if m not in cells]
    if missing:
        raise ValueError(f"stats.metrics not found in the cells table: {missing}")
    vdir = step_dir(cfg, 9)
    ks: list[int] = []
    if (vdir / MANIFEST).is_file():
        for k in cfg.vampire.k:
            f = vdir / f"shape_modes_K{int(k)}.csv"
            if f.is_file():
                mk = pd.read_csv(f)[["image", "label", "shape_mode"]].rename(columns={"shape_mode": f"shape_mode_K{int(k)}"})
                cells = cells.merge(mk, on=["image", "label"], how="left", validate="1:1")
                ks.append(int(k))
    else:
        LOG.info("step 9 (vampire) has not been run: shape-mode composition is skipped")
    d = prepare_step_dir(cfg, 10, ctx.overwrite)
    fig_dir = d / "figures"
    apply_style(cfg.plot)
    colors = {c: cfg.conditions.colors.get(c, cfg.conditions.default_color) for c in conds}
    cells.to_csv(d / "object_metrics.csv", index=False)
    sig = "p_welch_vs_reference" if st.significance_from == "p" else "q_bh"
    summ = []
    for m in metrics:
        s = summarise(cells, m, conds, ref)
        summ.append(s)
        stars_map = {r.condition: ("" if r.condition == ref else stars(getattr(r, sig), st.star_thresholds))
                     for r in s.itertuples()}
        data = {c: cells.loc[cells.condition == c, m].dropna().to_numpy(float) for c in conds}
        data = {c: v for c, v in data.items() if len(v)}
        fig, src = box_jitter(data, colors, AXIS_LABELS.get(m, m), stars_map, st.box)
        src.insert(0, "metric", m)
        src["test"] = f"object-level Welch t-test vs {ref}; stars from {sig}"
        save_figure(fig, fig_dir, f"box_{m}", src, cfg.plot.dpi)
    summary = pd.concat(summ, ignore_index=True)
    summary["stars"] = [
        ("" if c == ref else stars(v, st.star_thresholds)) for c, v in zip(summary.condition, summary[sig], strict=True)
    ]
    summary.to_csv(d / "summary_by_condition.csv", index=False)
    comp_counts = {}
    for k in ks:
        col = f"shape_mode_K{k}"
        mm = cells.dropna(subset=[col]).astype({col: int}).rename(columns={col: "shape_mode"})
        comp = composition(mm, conds, ref, k)
        comp_sig = comp["p_fisher" if st.significance_from == "p" else "q_bh"]
        comp["stars"] = [
            ("" if c == ref else stars(v, st.star_thresholds)) for c, v in zip(comp.condition, comp_sig, strict=True)
        ]
        comp.to_csv(d / f"mode_composition_K{k}.csv", index=False)
        frac = comp.pivot(index="condition", columns="mode", values="fraction").reindex(conds)
        n = comp.drop_duplicates("condition").set_index("condition").n_condition
        fig, src = stacked_fraction_bars(frac, n, cfg.vampire.mode_colors, st.box.line_width_pt)
        save_figure(fig, fig_dir, f"mode_fraction_stacked_K{k}", src, cfg.plot.dpi)
        fig, src = per_mode_fraction_bars(comp, conds, colors, st.box.line_width_pt,
                                          "p_fisher" if st.significance_from == "p" else "q_bh", st.star_thresholds)
        save_figure(fig, fig_dir, f"mode_fraction_per_mode_K{k}", src, cfg.plot.dpi)
        comp_counts[f"K{k}"] = int(len(mm))
        if st.shannon.enabled:
            _shannon(cfg, d, fig_dir, mm, conds, k)
    write_manifest(d, cfg, 10, {"stats": dataclasses.asdict(st), "reference": ref, "conditions": conds,
                                "pixel_size_um_effective": um_per_px, "input_tables_from_step": from_step},
                   {"objects": len(cells), "objects_per_condition": cells.groupby("condition").size().to_dict()},
                   {"metrics": metrics, "box_plots": len(metrics), "composition_objects": comp_counts})
    LOG.info("step 10 done: %d objects, %d metrics, modes K=%s -> %s", len(cells), len(metrics), ks or "none", d)


def _shannon(cfg: Config, d, fig_dir, mm: pd.DataFrame, conds: list[str], k: int) -> None:
    import matplotlib.pyplot as plt

    sh = cfg.stats.shannon
    rng = np.random.default_rng(sh.seed)
    rows = []
    for i, c in enumerate(conds):
        v = mm.loc[mm.condition == c, "shape_mode"]
        if len(v) == 0:
            continue
        h, lo, hi = shannon(v, k, sh.n_boot, rng, sh.ci)
        rows.append({"K": k, "condition": c, "x": i, "n": len(v), "shannon_H": h, "ci_low": lo, "ci_high": hi,
                     "n_boot": sh.n_boot, "ci_percent": sh.ci, "seed": sh.seed})
    t = pd.DataFrame(rows)
    t.to_csv(d / f"shannon_K{k}.csv", index=False)
    fig, ax = plt.subplots(figsize=(0.95 + 0.45 * len(t) + 0.3, 2.8))
    ax.errorbar(t.x, t.shannon_H, yerr=[t.shannon_H - t.ci_low, t.ci_high - t.shannon_H], fmt="o", color="k",
                ms=3, lw=cfg.stats.box.line_width_pt * 2, capsize=2)
    ax.set_xticks(t.x)
    ax.set_xticklabels([f"{c}\n(n={n})" for c, n in zip(t.condition, t.n, strict=True)], rotation=45, ha="right")
    ax.set_ylabel(f"Shannon diversity of modes (K={k})")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    save_figure(fig, fig_dir, f"shannon_K{k}", t, cfg.plot.dpi)
