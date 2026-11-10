"""Proposal / decision protocol, decision cards, snapshots and the report builder (synthetic data only)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest
import yaml
from PIL import Image

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from morphoagent.mcp import stage_tools as st  # noqa: E402
from morphoagent.pipeline.cli import main  # noqa: E402
from morphoagent.pipeline.config import load_config  # noqa: E402
from morphoagent.pipeline.plotting import save_figure  # noqa: E402
from morphoagent.pipeline.ui.build import static_text  # noqa: E402
from morphoagent.pipeline.ui.server import make_server  # noqa: E402
from morphoagent.records import protocol as rp  # noqa: E402
from morphoagent.records.report import build_report  # noqa: E402
from morphoagent.records.snapshots import SNAPSHOT_FILES  # noqa: E402

from .conftest import REPO  # noqa: E402

NODE = shutil.which("node")
TOKEN = "test-token-0123456789"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def rec_config(tmp_path: Path) -> Path:
    """Configuration with no pipeline step run (only the records folder is used)."""
    p = tmp_path / "rec.yaml"
    p.write_text(yaml.safe_dump({"output_dir": str(tmp_path / "out"), "conditions": {"order": ["CTRL", "DRUG"]}}))
    return p


def plot_figure(d: Path, name: str, values: list[float], tool: str = "cut_distributions") -> Path:
    """Bar plot saved as PNG + SVG + source CSV, with a run_manifest.json in the same folder."""
    fig, ax = plt.subplots(figsize=(3, 2))
    x = np.arange(len(values))
    ax.bar(x, values, label=f"cells (n={len(values)})")
    ax.legend()
    src = pd.DataFrame({"x": x, "y": values})
    save_figure(fig, d, name, src, 300)
    (d / "run_manifest.json").write_text(json.dumps({"tool": tool, "parameters": {"area_min_px": [100, 200]}}))
    return d / f"{name}.png"


def raster_figure(d: Path, name: str) -> Path:
    """A plain RGB PNG without SVG or source table (like an image montage)."""
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    p = d / f"{name}.png"
    Image.fromarray(rng.integers(0, 255, (60, 90, 3), dtype=np.uint8)).save(p)
    return p


def two_options(fig_a: Path, fig_b: Path) -> list[dict]:
    return [
        {"id": "A", "label": "area >= 100 px^2", "figures": [str(fig_a)], "numbers": {"n_kept": 120, "fraction": 0.81},
         "table": [{"condition": "CTRL", "n_kept": 60}, {"condition": "DRUG", "n_kept": 60}],
         "references": ["pmid 12345"], "config_changes": {"select.area_min_px": 100}},
        {"id": "B", "label": "area >= 200 px^2", "figures": [str(fig_b)], "numbers": {"n_kept": 90, "fraction": 0.6},
         "references": ["https://doi.org/10.1000/xyz.1"], "config_changes": {"select.area_min_px": 200}},
    ]


def _req(url: str, data: dict | None = None, token: str | None = TOKEN) -> tuple[int, dict]:
    h = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        h["X-BFM-Token"] = token
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def live(rec_config: Path, monkeypatch: pytest.MonkeyPatch):
    """Review server on an ephemeral loopback port, token required, no pipeline step run."""
    monkeypatch.setenv("MORPHOAGENT_UI_TOKEN", TOKEN)
    cfg = load_config(rec_config)
    httpd, app = make_server(cfg, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg
    httpd.shutdown()
    httpd.server_close()


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_reference_normalisation() -> None:
    assert rp.normalize_reference("PMID 7564209") == "PMID:7564209"
    assert rp.normalize_reference("pmid:00123") == "PMID:123"
    assert rp.normalize_reference("doi:10.1038/s0000-000-0") == "doi:10.1038/s0000-000-0"
    assert rp.normalize_reference("https://doi.org/10.1000/abc") == "doi:10.1000/abc"
    for bad in ("Smith 2020", "PMID:", "doi:11.1/x", ""):
        with pytest.raises(ValueError):
            rp.normalize_reference(bad)


def test_schema_validation() -> None:
    ok = {"id": "A", "label": "x"}
    assert rp.OptionIn.model_validate(ok).figures == []
    for bad in ({"id": "rejected", "label": "x"}, {"id": "a b", "label": "x"}, {"id": "A", "label": ""},
                {"id": "A", "label": "x", "references": ["not a ref"]}, {"id": "A", "label": "x", "extra": 1}):
        with pytest.raises(ValueError):
            rp.OptionIn.model_validate(bad)
    assert rp.Evidence.model_validate("text only").text == "text only"
    base = {"schema": rp.PROPOSAL_SCHEMA, "proposal_id": "cut_20261107T101500Z_abc123", "stage": "cut", "title": "t",
            "created_utc": "2026-11-07T10:15:00+00:00", "options": [ok]}
    p = rp.Proposal.model_validate(base)
    assert p.dump()["schema"] == rp.PROPOSAL_SCHEMA
    for change in ({"options": [ok, ok]}, {"recommended": "Z"}, {"stage": "segment"}, {"stage": "magic"},
                   {"schema": "morphoagent-proposal/2"}, {"options": []}):
        with pytest.raises(ValueError):
            rp.Proposal.model_validate({**base, **change})
    dec = {"schema": rp.DECISION_SCHEMA, "proposal_id": base["proposal_id"], "stage": "cut", "choice": "A",
           "decided_utc": "2026-11-07T10:16:00+00:00", "proposal_sha256": "0" * 64}
    assert rp.Decision.model_validate(dec).source == "api"
    for change in ({"proposal_sha256": "abc"}, {"source": "someone"}, {"proposal_id": "../x"}):
        with pytest.raises(ValueError):
            rp.Decision.model_validate({**dec, **change})


def _keys(schema: dict, defs: dict) -> dict:
    """Property names and required keys of a schema and of every referenced definition."""
    out = {"properties": sorted(schema.get("properties", {})), "required": sorted(schema.get("required", []))}
    for name, d in sorted(defs.items()):
        out[name] = {"properties": sorted(d.get("properties", {})), "required": sorted(d.get("required", []))}
    return out


@pytest.mark.parametrize(("model", "file"), [(rp.Proposal, "proposal-1.schema.json"), (rp.Decision, "decision-1.schema.json")])
def test_documented_json_schema_matches_models(model, file: str) -> None:
    shipped = json.loads((REPO / "docs" / "schemas" / file).read_text())
    current = model.model_json_schema(by_alias=True)
    assert _keys(shipped, shipped.get("$defs", {})) == _keys(current, current.get("$defs", {}))


# --------------------------------------------------------------------------- #
# propose / guard
# --------------------------------------------------------------------------- #
def test_propose_copies_figures_and_reads_provenance(rec_config: Path, tmp_path: Path) -> None:
    cfg = load_config(rec_config)
    fa = plot_figure(tmp_path / "runA", "cut_histograms", [3, 5, 2])
    fb = raster_figure(tmp_path / "runB", "montage")
    r = rp.propose(cfg, "cut", "Minimum cell area", two_options(fa, fb), recommended="A", rationale="keeps 81 %",
                   evidence=["120 cells kept (cut_table.csv)"])
    pid = r["proposal_id"]
    assert rp.PROPOSAL_ID_RE.match(pid) and pid.startswith("cut_")
    prop, sha = rp.load_proposal(cfg, pid)
    assert sha == r["sha256"] == _sha(Path(r["path"]))
    files = rp.records_dir(cfg) / "proposals" / f"{pid}_files"
    assert sorted(p.name for p in files.iterdir()) == ["A__cut_histograms.png", "A__cut_histograms.svg", "B__montage.png",
                                                        "source_A__cut_histograms.csv"]
    assert prop.option("A").figures == [f"{pid}_files/A__cut_histograms.png"]
    assert prop.option("A").references == ["PMID:12345"] and prop.option("B").references == ["doi:10.1000/xyz.1"]
    assert prop.main_figure == f"{pid}_files/A__cut_histograms.png"
    assert prop.provenance.tool == "cut_distributions" and prop.provenance.parameters == {"area_min_px": [100, 200]}
    # later changes to the original figure do not change what the researcher sees
    before = _sha(files / "A__cut_histograms.png")
    fa.write_bytes(b"changed")
    assert _sha(files / "A__cut_histograms.png") == before
    assert rp.get_decision(cfg, pid) is None
    assert [p["proposal_id"] for p in rp.list_pending(cfg)] == [pid]
    man = json.loads((rp.records_dir(cfg) / "run_manifest.json").read_text())
    assert man["counts_out"]["pending"] == 1 and man["events"][-1]["action"] == "propose"
    with pytest.raises(rp.RecordsError, match="not found"):
        rp.propose(cfg, "cut", "t", [{"id": "A", "label": "x", "figures": [str(tmp_path / "none.png")]}])
    with pytest.raises(rp.RecordsError, match="stage"):
        rp.propose(cfg, "magic", "t", [{"id": "A", "label": "x"}])
    with pytest.raises(FileExistsError):  # proposal files are never replaced
        rp.write_new_json(Path(r["path"]), {})
    assert _sha(Path(r["path"])) == sha


def test_guard_blocks_later_stage_until_decided(config_file: Path) -> None:
    cfg = load_config(config_file)
    r = rp.propose(cfg, "consult", "Conditions and reference", [{"id": "ok", "label": "CTRL is the reference"}])
    rp.require_decision(cfg, "consult")  # same stage: not blocked
    with pytest.raises(rp.DecisionRequiredError, match=r["proposal_id"]):
        rp.require_decision(cfg, "contrast")
    with pytest.raises(rp.DecisionRequiredError):
        st.preview_enhance(str(config_file))
    assert not (Path(cfg.output_dir) / "02_enhance-preview").exists()  # refused before writing anything
    rp.decide(cfg, r["proposal_id"], "ok", "fine")
    out = st.preview_enhance(str(config_file))
    assert Path(out["contact_sheet"]).is_file()
    # a later proposal of the same stage does not block; switching the guard off releases everything
    r2 = rp.propose(cfg, "contrast", "Preset", [{"id": "fd", "label": "figure-default"}])
    with pytest.raises(rp.DecisionRequiredError):
        rp.require_decision(cfg, "cluster")
    config_file.write_text(config_file.read_text() + "agent:\n  require_decisions: false\n")
    rp.require_decision(load_config(config_file), "cluster")
    assert rp.get_decision(cfg, r2["proposal_id"]) is None


# --------------------------------------------------------------------------- #
# HTTP decision -> snapshot -> report
# --------------------------------------------------------------------------- #
def test_decide_over_http_snapshot_and_report(live, tmp_path: Path) -> None:
    base, cfg = live
    root = rp.records_dir(cfg)
    fa = plot_figure(tmp_path / "runA", "cut_histograms", [3, 5, 2])
    fb = raster_figure(tmp_path / "runB", "montage")
    p1 = rp.propose(cfg, "cut", "Minimum cell area", two_options(fa, fb), recommended="A")
    p2 = rp.propose(cfg, "contrast", "Display preset", [
        {"id": "m", "label": "montage", "figures": [str(fb)]}, {"id": "n", "label": "none"}])
    p3 = rp.propose(cfg, "cluster", "Number of shape modes", [
        {"id": "K3", "label": "K = 3", "numbers": {"inertia": 10.5}},
        {"id": "K4", "label": "K = 4", "numbers": {"inertia": 8.1}}], recommended="K4")
    # token protection is unchanged
    assert _req(base + "/api/proposals", token=None)[0] == 401
    st_, doc = _req(base + "/api/proposals")
    assert st_ == 200 and [r["proposal"]["proposal_id"] for r in doc["proposals"]] == [p1["proposal_id"], p2["proposal_id"],
                                                                                        p3["proposal_id"]]
    first = doc["proposals"][0]
    assert first["decision"] is None and first["proposal"]["options"][0]["thumbs"][0].startswith("data:image/jpeg;base64,")
    assert "figure_origins" not in first["proposal"]["options"][0] and str(tmp_path) not in json.dumps(doc)
    # refused requests
    req = {"proposal_id": p1["proposal_id"], "choice": "A", "comment": "keeps most cells", "proposal_sha256": p1["sha256"]}
    assert _req(base + "/api/decide", {**req, "proposal_sha256": "0" * 64})[0] == 409
    assert _req(base + "/api/decide", {**req, "choice": "Z"})[0] == 400
    assert _req(base + "/api/decide", {**req, "proposal_id": "cut_20000101T000000Z_000000"})[0] == 404
    assert _req(base + "/api/decide", {**req, "proposal_id": "../../etc"})[0] == 404
    assert not (root / "decisions").exists()
    # accepted
    code, res = _req(base + "/api/decide", req)
    assert code == 200 and res["choice"] == "A"
    dec = json.loads((root / "decisions" / f"{p1['proposal_id']}.json").read_text())
    assert dec["schema"] == rp.DECISION_SCHEMA and dec["source"] == "review-ui" and dec["proposal_sha256"] == p1["sha256"]
    assert dec["comment"] == "keeps most cells" and rp.get_decision(cfg, p1["proposal_id"])["choice"] == "A"
    snap = root / dec["snapshot"]
    assert sorted(p.name for p in snap.iterdir()) == sorted(SNAPSHOT_FILES)
    assert re.match(r"^\d{8}T\d{6}Z_cut_minimum-cell-area$", snap.name)
    with Image.open(snap / "view.png") as im:
        assert im.mode == "RGBA" and round(im.info["dpi"][0]) == 300
    assert (snap / "view.svg").read_bytes() == (fa.with_suffix(".svg")).read_bytes()
    assert (snap / "source.csv").read_text() == (fa.parent / "source_cut_histograms.csv").read_text()
    note = json.loads((snap / "note.json").read_text())
    assert (note["kind"], note["stage"], note["choice"], note["comment"]) == ("decision", "cut", "A", "keeps most cells")
    assert note["proposal"]["proposal_id"] == p1["proposal_id"]
    snap_hashes = {p.name: _sha(p) for p in snap.iterdir()}
    # decisions are final
    assert _req(base + "/api/decide", {**req, "choice": "B"})[0] == 409
    with pytest.raises(rp.DecisionExistsError):
        rp.decide(cfg, p1["proposal_id"], "B")
    assert json.loads((root / "decisions" / f"{p1['proposal_id']}.json").read_text())["choice"] == "A"
    # rejection of a raster-figure proposal: SVG embeds the PNG, source.csv is the options table
    code, res = _req(base + "/api/decide", {"proposal_id": p2["proposal_id"], "choice": "rejected", "comment": "",
                                            "proposal_sha256": p2["sha256"]})
    assert code == 200
    s2 = root / res["snapshot"]
    assert "data:image/png;base64," in (s2 / "view.svg").read_text()
    assert list(pd.read_csv(s2 / "source.csv").option) == ["m", "n"]
    # proposal without figures: the options table is drawn
    code, res = _req(base + "/api/decide", {"proposal_id": p3["proposal_id"], "choice": "K4", "comment": "elbow",
                                            "proposal_sha256": p3["sha256"]})
    s3 = root / res["snapshot"]
    assert code == 200 and sorted(p.name for p in s3.iterdir()) == sorted(SNAPSHOT_FILES)
    assert "<svg" in (s3 / "view.svg").read_text() and list(pd.read_csv(s3 / "source.csv").option) == ["K3", "K4"]
    # manual snapshots: never overwrite, a second one gets a new folder
    code, m1 = _req(base + "/api/snapshot", {"proposal_id": p1["proposal_id"], "option_id": "B", "comment": "for the deck"})
    code2, m2 = _req(base + "/api/snapshot", {"proposal_id": p1["proposal_id"], "option_id": "B", "comment": "again"})
    assert code == code2 == 200 and m1["snapshot"] != m2["snapshot"] and sorted(m1["files"]) == sorted(SNAPSHOT_FILES)
    assert _req(base + "/api/snapshot", {"proposal_id": p1["proposal_id"], "option_id": "Z"})[0] == 400
    assert {p.name: _sha(p) for p in snap.iterdir()} == snap_hashes
    n_snap = len(list((root / "snapshots").iterdir()))
    assert n_snap == 5
    # report
    out = tmp_path / "report"
    r1 = build_report(cfg, out, prefix="exp01")
    assert re.match(r"^exp01_report_\d{6}_c1\.pptx$", Path(r1["pptx"]).name) and r1["n_slides"] == n_snap
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation(r1["pptx"])
    assert len(prs.slides) == n_snap and abs(prs.slide_width / prs.slide_height - 16 / 9) < 0.01
    stages, table_only = [], []
    for slide in prs.slides:
        tables = [sh for sh in slide.shapes if sh.has_table]
        n_pic = sum(1 for sh in slide.shapes if sh.shape_type == 13)  # 13 = picture
        assert len(tables) == 1 and n_pic in (0, 1)
        if n_pic == 0:  # figure-less proposal: no drawing of the table, the real table spans the slide width
            table_only.append(slide)
            assert tables[0].left < Inches(1) and tables[0].width > Inches(6.5)
        texts = " ".join(sh.text_frame.text for sh in slide.shapes if sh.has_text_frame)
        assert "proposal" in texts and "stage" in texts
        stages.append(texts.split(":")[0])
    assert stages[0].startswith("Stage 1") and stages[-1].startswith("Stage 3")  # stage order, not time order
    assert len(table_only) == 1  # only the figure-less K proposal
    cut_slide = prs.slides[1]
    tab = next(sh for sh in cut_slide.shapes if sh.has_table).table
    assert [tab.cell(0, j).text for j in range(4)] == ["mark", "option", "label", "n kept"]
    assert tab.cell(1, 0).text == "chosen, recommended"
    md = Path(r1["markdown"]).read_text()
    links = re.findall(r"!\[figure\]\(([^)]+)\)", md)
    assert len(links) == n_snap and all((out / link).is_file() for link in links)
    assert "https://pubmed.ncbi.nlm.nih.gov/12345/" in md
    first_hash = _sha(Path(r1["pptx"]))
    r2 = build_report(cfg, out, prefix="exp01")
    assert Path(r2["pptx"]).name.endswith("_c2.pptx") and _sha(Path(r1["pptx"])) == first_hash
    man = json.loads((out / "run_manifest.json").read_text())
    assert [x["number"] for x in man["reports"]] == [1, 2]


def test_report_without_snapshots_fails(rec_config: Path, tmp_path: Path) -> None:
    assert main(["report", "-c", str(rec_config), "--out", str(tmp_path / "r")]) == 1
    assert not (tmp_path / "r").exists() or not any((tmp_path / "r").iterdir())


# --------------------------------------------------------------------------- #
# Offline page
# --------------------------------------------------------------------------- #
def test_ui_build_embeds_proposals_without_pipeline(rec_config: Path, tmp_path: Path) -> None:
    cfg = load_config(rec_config)
    rp.propose(cfg, "cut", "Minimum cell area", two_options(plot_figure(tmp_path / "a", "h", [1, 2]),
                                                            raster_figure(tmp_path / "b", "m")))
    out = tmp_path / "review.html"
    assert main(["ui", "build", "-c", str(rec_config), "-o", str(out)]) == 0
    html = out.read_text()
    m = re.search(r'<script id="bfm-data" type="application/json">(.*?)</script>', html, re.S)
    doc = json.loads(m.group(1).replace("<\\/", "</"))
    assert doc["fields"] == [] and doc["fields_reason"]
    props = doc["decisions"]["proposals"]
    assert len(props) == 1 and props[0]["proposal"]["options"][1]["thumbs"][0].startswith("data:image/jpeg;base64,")
    assert str(tmp_path) not in json.dumps(doc)
    assert "5. Decisions" in html and not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', html, re.I)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_offline_download_imports_through_cli(rec_config: Path, tmp_path: Path) -> None:
    cfg = load_config(rec_config)
    p = rp.propose(cfg, "segment", "Cellpose setting", [{"id": "s1", "label": "flow 0.8"}, {"id": "s2", "label": "flow 0.4"}])
    entry = rp.list_proposals(cfg)[0]
    core = static_text("app.js").split("/* END curation-core */")[0]
    script = core + (f"\nvar e = {json.dumps(entry)};\nconsole.log(JSON.stringify(["
                     "BfmCore.decisionDoc(e, 's2', 'sharper outlines', '2026-11-07T10:20:30.123Z', '0.1.0'),"
                     "BfmCore.decisionDoc(e, 'zz', '', '2026-11-07T10:20:30.123Z', '0.1.0'),"
                     "BfmCore.refUrl('PMID:42', {pmid: 'P/', doi: 'D/'}), BfmCore.refUrl('doi:10.1/x', {pmid: 'P/', doi: 'D/'}),"
                     "BfmCore.refUrl('junk', {pmid: 'P/', doi: 'D/'})]));\n")
    out = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)
    doc, bad, u1, u2, u3 = out
    assert bad is None and (u1, u2, u3) == ("P/42/", "D/10.1/x", None)
    rp.Decision.model_validate(doc)
    f = tmp_path / f"{p['proposal_id']}.json"
    f.write_text(json.dumps(doc))
    assert main(["decision", "-c", str(rec_config), "--import", str(f)]) == 0
    d = rp.get_decision(cfg, p["proposal_id"])
    assert (d["choice"], d["source"], d["decided_utc"]) == ("s2", "review-ui-download", "2026-11-07T10:20:30+00:00")
    assert sorted(x.name for x in (rp.records_dir(cfg) / d["snapshot"]).iterdir()) == sorted(SNAPSHOT_FILES)
    assert main(["decision", "-c", str(rec_config), "--import", str(f)]) == 1  # already decided


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #
def test_mcp_protocol_tools(config_file: Path, tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError

    from morphoagent.mcp.pipeline_server import server

    fig = plot_figure(tmp_path / "run", "h", [1, 2, 3])
    args = {"config_path": str(config_file), "stage": "consult", "title": "Reference condition",
            "options": [{"id": "ctrl", "label": "CTRL", "figures": [str(fig)], "references": ["PMID:1"]}],
            "recommended": "ctrl", "evidence": ["two conditions found"]}
    res = asyncio.run(server.call_tool("propose", args))
    pid = res.structured_content["proposal_id"]
    pending = asyncio.run(server.call_tool("list_pending", {"config_path": str(config_file)})).structured_content
    assert pending["n_pending"] == 1 and pending["pending"][0]["proposal_id"] == pid
    got = asyncio.run(server.call_tool("get_decision", {"config_path": str(config_file), "proposal_id": pid})).structured_content
    assert got == {"proposal_id": pid, "decided": False, "decision": None}
    with pytest.raises(ToolError, match="blocked"):
        asyncio.run(server.call_tool("preview_enhance", {"config_path": str(config_file)}))
    with pytest.raises(ToolError, match="rejected"):
        asyncio.run(server.call_tool("propose", {**args, "options": [{"id": "rejected", "label": "x"}]}))
    rp.decide(load_config(config_file), pid, "ctrl")
    got = asyncio.run(server.call_tool("get_decision", {"config_path": str(config_file), "proposal_id": pid})).structured_content
    assert got["decided"] and got["decision"]["choice"] == "ctrl"
    rep = asyncio.run(server.call_tool("build_report", {"config_path": str(config_file), "out_dir": str(tmp_path / "rep")}))
    assert rep.structured_content["n_slides"] == 1 and Path(rep.structured_content["pptx"]).is_file()


def test_slide_layout_comment_below_table_and_min_column_width(tmp_path: Path) -> None:
    import pandas as pd
    from pptx import Presentation
    from pptx.util import Inches

    from morphoagent.records.report import MIN_COL_IN, SLIDE_H_IN, SLIDE_W_IN, TEXT_BOTTOM_IN, _fmt, _slide

    assert _fmt(3.14159265) == "3.14" and _fmt(0.000123456) == "0.000123" and _fmt(12345.6) == "12346"
    assert _fmt(float("nan")) == ""  # an option without this number shows an empty cell, not "nan"
    img = tmp_path / "f.png"
    Image.fromarray(np.zeros((40, 60), np.uint8)).save(img)
    cols = ["edge_strength", "flat_noise", "p50", "background_level", "contrast_range", "saturation_fraction", "extra_metric"]
    rows = [{"mark": "chosen" if i == 0 else "", "option": f"preset-{i}", "label": f"a fairly long label for preset number {i}",
             **{c: 1.23456789 * (j + 1) for j, c in enumerate(cols)}} for i in range(8)]
    content = {"title": "Stage 1: t", "decision": "Chosen: preset-0", "comment": "word " * 120,
               "rationale": "because " * 200, "table": pd.DataFrame(rows), "hidden_rows": 0,
               "references": ["PMID:12345"], "provenance": "prov", "image": img}
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W_IN), Inches(SLIDE_H_IN)
    geo = _slide(prs, content, "Arial", 6)
    path = tmp_path / "t.pptx"
    prs.save(path)
    slide = Presentation(path).slides[0]
    gf = next(sh for sh in slide.shapes if sh.has_table)
    table = gf.table
    heights = sum(r.height for r in table.rows) / 914400
    assert len(table.columns) <= 3 + 6
    assert all(c.width / 914400 >= MIN_COL_IN - 1e-6 for c in table.columns)
    assert sum(c.width for c in table.columns) / 914400 <= 6.0 + 1e-6
    box = next(sh for sh in slide.shapes if sh.has_text_frame and sh.left > Inches(6) and sh.top > gf.top)
    assert box.top / 914400 >= gf.top / 914400 + heights - 1e-6
    assert (box.top + box.height) / 914400 <= TEXT_BOTTOM_IN + 1e-6
    assert geo["box_top"] >= geo["table_top"] + geo["table_h"]
    assert "because" in slide.notes_slide.notes_text_frame.text  # the full text stays in the notes
    assert all("_" not in table.cell(0, j).text for j in range(len(table.columns)))


# --------------------------------------------------------------------------- #
# Path redaction (agent.report.redact_paths)
# --------------------------------------------------------------------------- #
def test_redact_paths_rules(tmp_path: Path) -> None:
    from morphoagent.records.report import redact_paths

    base = tmp_path / "cfg"
    assert redact_paths("/opt/tools/VAMPIRE_open/Source Code", base) == "<path>/Source Code"  # whole-string path
    assert redact_paths(str(base / "inputs" / "fields.csv"), base) == "inputs/fields.csv"  # inside the config folder
    assert redact_paths("python /usr/local/envs/v/bin/python ran", base) == "python <path>/python ran"
    assert redact_paths(r"file C:\Users\someone\a.png here") == "file <path>/a.png here"
    kept = "see https://doi.org/10.1038/s41596-020-00432-x, 1/8 grid, 0.24 um/px"
    assert redact_paths(kept, base) == kept  # URLs, fractions and units are not paths
    assert redact_paths({"k": ["/a/b/c.txt", 3]}, None) == {"k": ["<path>/c.txt", 3]}


@pytest.mark.parametrize("redact", [True, False])
def test_report_redacts_absolute_paths(rec_config: Path, tmp_path: Path, redact: bool) -> None:
    secret = "/srv/private-lab/vampire/Source Code"
    cfg_data = yaml.safe_load(rec_config.read_text())
    cfg_data["agent"] = {"report": {"redact_paths": redact}}
    rec_config.write_text(yaml.safe_dump(cfg_data))
    cfg = load_config(rec_config)
    p = rp.propose(cfg, "cut", "Minimum cell area", two_options(plot_figure(tmp_path / "a", "h", [1, 2]),
                                                                 plot_figure(tmp_path / "b", "g", [2, 3])),
                   recommended="A", rationale=f"runner {secret} and table {tmp_path / 'a' / 'h.png'}",
                   provenance={"tool": "run_vampire", "parameters": {"src_path": secret,
                                                                     "python": "/opt/envs/v/bin/python"}})
    rp.decide(cfg, p["proposal_id"], "A", comment="checked /srv/private-lab/logs/run.log")
    r = build_report(cfg, tmp_path / "rep", prefix="red")
    md = Path(r["markdown"]).read_text()
    from pptx import Presentation

    texts = " ".join(sh.text_frame.text for s in Presentation(r["pptx"]).slides for sh in s.shapes if sh.has_text_frame)
    if redact:
        for txt in (md, texts):
            assert "/srv/" not in txt and "/opt/envs" not in txt and str(tmp_path) not in txt
        assert "<path>/Source Code" in md and "<path>/python" in md and "<path>/run.log" in md
    else:
        assert secret in md and "/opt/envs/v/bin/python" in md
    note = next((Path(cfg.agent.records_dir or Path(cfg.output_dir) / "agent" / "records")).rglob("note.json"), None)
    if note is not None:  # full paths stay in the snapshot record
        assert secret in note.read_text()
    man = json.loads((tmp_path / "rep" / "run_manifest.json").read_text())
    assert man["parameters"]["redact_paths"] is redact


def test_decisions_tab_payload_has_no_absolute_paths(rec_config: Path, tmp_path: Path) -> None:
    from morphoagent.pipeline.ui.dataset import collect_decisions

    cfg = load_config(rec_config)
    rp.propose(cfg, "cut", "Minimum cell area", two_options(plot_figure(tmp_path / "a", "h", [1, 2]),
                                                            plot_figure(tmp_path / "b", "g", [2, 3])),
               rationale="python /opt/envs/v/bin/python wrote it",
               provenance={"tool": "run_vampire", "parameters": {"src_path": "/srv/lab/VAMPIRE/Source Code"}})
    doc = json.dumps(collect_decisions(cfg))
    assert "/opt/envs" not in doc and "/srv/" not in doc and str(tmp_path) not in doc
    assert "<path>/Source Code" in doc and "<path>/python" in doc
    payload = collect_decisions(cfg)
    limits = (payload["table_max_rows"], payload["table_max_columns"])
    assert limits == (cfg.ui.card_table_max_rows, cfg.ui.card_table_max_columns)


def test_review_page_collapses_details_and_extra_columns() -> None:
    """Provenance / configuration JSON sits in collapsed <details>; extra table columns are hidden, not dropped."""
    from morphoagent.pipeline.ui import dataset

    static = Path(dataset.__file__).parent / "static"
    js, css = (static / "app.js").read_text(), (static / "app.css").read_text()
    assert "<details class=\"det small\"><summary>" in js and "<details open" not in js
    assert "JSON.stringify(prov, null, 1)" in js and ".slice(0, 400)" not in js
    assert "DEC.table_max_columns" in js and "xcol" in js
    assert ".tw:not(.showall) .xcol{display:none}" in css


def test_report_number_continues_across_dates(tmp_path: Path) -> None:
    from morphoagent.records.report import _next_stem

    (tmp_path / "exp_report_261107_c1.pptx").write_bytes(b"")
    (tmp_path / "exp_report_261107_c2_files").mkdir()
    (tmp_path / "other_report_261107_c7.pptx").write_bytes(b"")
    assert _next_stem(tmp_path, "exp", "261108") == ("exp_report_261108_c3", 3)
