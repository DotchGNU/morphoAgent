"""Draw the morphoAgent architecture overview.

Usage: python make_architecture.py [OUT_DIR]   (default: this folder)
Writes architecture.svg and architecture.png (300 dpi, transparent).
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"]
matplotlib.rcParams["svg.fonttype"] = "none"

INK, MUTE = "#2b3a42", "#6b7b84"
C = {"human": "#f3e3c3", "ui": "#d6e6dc", "rec": "#dde3ee", "agent": "#e6dcee",
     "mcp": "#d3e4ea", "eng": "#e9e4d8", "ext": "#eeeeee", "grey": "#f2f2f2"}

fig, ax = plt.subplots(figsize=(18, 10.2))
ax.set_xlim(0, 180)
ax.set_ylim(-10, 102)
ax.axis("off")


def box(x, y, w, h, title, lines=(), fc="#fff", ec=INK, ls="-", tc=INK, fs=10.5, lfs=9):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=1.2",
                                fc=fc, ec=ec, lw=1.1, ls=ls))
    ax.text(x + w / 2, y + h - 2.0, title, ha="center", va="top", fontsize=fs, fontweight="bold", color=tc)
    ty = y + h - 5.2
    for ln in lines:
        ax.text(x + w / 2, ty, ln, ha="center", va="top", fontsize=lfs, color=tc)
        ty -= 3.0


def arrow(p, q, label=None, lp=None, both=False, ls="-", color=INK, rad=0.0, ha="center"):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="<|-|>" if both else "-|>", mutation_scale=13,
                                 lw=1.2, color=color, ls=ls, connectionstyle=f"arc3,rad={rad}"))
    if label:
        lx, ly = lp or ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        ax.text(lx, ly, label, ha=ha, va="center", fontsize=8.5, color=MUTE,
                bbox={"fc": "white", "ec": "none", "pad": 1.0, "alpha": 0.9})


# ---- column A: researcher + review UI
box(2, 78, 34, 18, "Researcher", ["decides every stage", "accepts / rejects + comment"], C["human"])
box(2, 40, 34, 32, "Review UI", ["local HTML page", "decision cards", "interactive figures [beta]", "snapshot button"], C["ui"])
arrow((19, 72), (19, 78), "decides", (27, 75), both=True)

# ---- column B: records
ax.add_patch(FancyBboxPatch((46, 28), 50, 68, boxstyle="round,pad=0,rounding_size=1.2",
                            fc="#fafbfd", ec=MUTE, lw=1.0, ls="--"))
ax.text(71, 94, "Records (files on disk)", ha="center", va="top", fontsize=10.5, fontweight="bold", color=INK)
box(50, 78, 42, 10, "proposals/*.json", ["written by the agent"], C["rec"])
box(50, 62, 42, 10, "decisions/*.json", ["written by the researcher"], C["rec"])
box(50, 42, 42, 14, "snapshots/", ["png, svg, source.csv", "state.json, note.json"], C["rec"])
box(50, 30, 42, 8, "PPTX deck + Markdown report", [], C["human"], fs=9.5)
arrow((71, 78), (71, 72), None)
arrow((71, 62), (71, 56), None)
arrow((71, 42), (71, 38), "morphoagent report", (71, 40.0), ha="center")

# UI <-> records
arrow((36, 58), (50, 65), "decide", (42.5, 61.2), color=INK)
arrow((50, 81), (36, 70), "cards", (43, 78.5), color=INK)
arrow((36, 47), (50, 47), "snapshot", (43, 49), color=INK)

# ---- column C: agent + MCP
box(104, 80, 36, 16, "Agent client (MCP host)", ["reads repo agent instructions", "+ stage skills"], C["agent"], fs=10)
MCP_TOOLS = ["(command morphoagent-mcp)", "stage tools:", "inspect_folder, preview_enhance,",
             "run_segment, cut_distributions,", "run_vampire", "records tools:", "propose, list_pending,",
             "get_decision, build_report"]
LIT_TOOLS = ["(command morphoagent-literature)", "search_literature, fetch_abstract,", "fetch_methods_text,",
             "verify_citation"]
box(104, 47, 36, 30, "morphoagent-pipeline", MCP_TOOLS, C["mcp"], lfs=8.3)
box(104, 27, 36, 17, "morphoagent-literature", LIT_TOOLS, C["mcp"], lfs=8.3)
arrow((122, 80), (122, 77), "tool calls", (131, 78.5), both=True)
arrow((122, 47), (122, 44), None, both=True)
arrow((104, 90), (92, 85), "propose", (98, 90.5))
arrow((92, 66), (104, 66), "get_decision", (98, 68.5), ls="-")
arrow((104, 58), (92, 58), None, ls="--", color=MUTE)
ax.text(98, 55.6, "read", ha="center", fontsize=8.0, color=MUTE)

# ---- column D: engine + external
ENGINE = ["morphoagent.pipeline (10 CLI steps)", "config-driven, resumable", "",
          "1 load   2 preview   3 enhance", "4 tune   5 segment   6 select",
          "7 review   8 curate   9 VAMPIRE", "10 stats + figures", "", "run_manifest.json per step"]
box(148, 62, 30, 34, "Engine", ENGINE, C["eng"], lfs=8.5)
box(148, 48, 30, 11, "Cellpose-SAM", ["segmentation"], C["eng"], fs=9.5)
box(148, 36, 30, 10, "VAMPIRE", ["shape modes (separate env)"], C["eng"], fs=9.5, lfs=8.3)
box(148, 22, 30, 7, "PubMed / Europe PMC", [], C["ext"], fs=9)
arrow((140, 64), (148, 80), "runs steps", (144, 74), both=False)
arrow((140, 40), (148, 25.5), "queries", (144, 31), both=False)
arrow((163, 62), (163, 59), None)
arrow((163, 48), (163, 46), None)

# CLI note
box(104, 14, 74, 7, "Works without an agent: every step is also a CLI command", [],
    "#ffffff", ec=MUTE, ls=":", tc=MUTE, fs=9)

# ---- stage strip
ax.text(2, 9.5, "Stages", fontsize=10.5, fontweight="bold", color=INK, va="center")
stages = [("0  Consult", "inspect_folder"), ("1  Contrast", "preview_enhance"),
          ("2  Segmentation\n& cut-offs", "run_segment, cut_distributions"), ("3  Shape modes", "run_vampire"),
          ("4  Literature-based\nrevision", "literature server"), ("5  Cell-state\nhypotheses", "planned")]
x0, w, gap, y, h = 2, 24.5, 5.2, -8, 14
for i, (t, s) in enumerate(stages):
    x = x0 + i * (w + gap)
    planned = i == 5
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=1.0",
                                fc=C["grey"] if planned else "#f7f1e3", ec=MUTE if planned else INK,
                                lw=1.1, ls="--" if planned else "-"))
    ax.text(x + w / 2, y + h - 2, t, ha="center", va="top", fontsize=9.5, fontweight="bold",
            color="#9a9a9a" if planned else INK, linespacing=1.1)
    ax.text(x + w / 2, y + 2.6, s, ha="center", va="bottom", fontsize=8, color="#9a9a9a" if planned else MUTE)
    if i < 5:
        gx = x + w + gap / 2
        ax.plot([gx, gx], [y + 1.5, y + h - 1.5], color="#b5483a", lw=2.2, solid_capstyle="round",
                ls=":" if i == 4 else "-")
ax.text(x0 + 2.5 * (w + gap) + w + gap / 2, 8.2, "red bar = decision gate: researcher decision required before the next stage",
        ha="center", va="center", fontsize=8.5, color="#b5483a")

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
fig.savefig(out / "architecture.svg", transparent=True, bbox_inches="tight")
fig.savefig(out / "architecture.png", dpi=300, transparent=True, bbox_inches="tight")
