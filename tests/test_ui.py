"""Browser helper tools: offline page builder, curation export round trip, live server, assistant."""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tifffile
from PIL import Image
from scipy import ndimage as ndi

from morphoagent.pipeline.cli import main
from morphoagent.pipeline.config import LLMConfig, load_config
from morphoagent.pipeline.overlay import grid_sheet
from morphoagent.pipeline.ui import llm
from morphoagent.pipeline.ui.build import static_text
from morphoagent.pipeline.ui.dataset import crop_montage_panels, decode_label_png, encode_label_png, resize_labels
from morphoagent.pipeline.ui.server import UIServerError, is_loopback, make_server, resolve_token

NODE = shutil.which("node")


class SplitRunner:
    """Threshold stand-in for Cellpose that splits the largest object into two touching labels."""

    def __init__(self, model: str = "cpsam", gpu: bool = False) -> None:
        self.gpu = False

    def __call__(self, img, flow_threshold, cellprob_threshold, diameter):
        lab, n = ndi.label(img > 150 - 5 * abs(cellprob_threshold))
        if n:
            sizes = np.bincount(lab.ravel())[1:]
            big = int(np.argmax(sizes)) + 1
            xs = np.where(lab == big)[1]
            lab[(lab == big) & (np.indices(lab.shape)[1] > np.median(xs))] = n + 1
        return lab.astype(np.int32), 0.0


@pytest.fixture
def pipeline(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Steps 1-6 on the synthetic images (largest object split into two touching labels)."""
    import morphoagent.pipeline.steps_image as si

    monkeypatch.setattr(si, "CellposeRunner", SplitRunner)
    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "6"]) == 0
    return config_file


def _doc(html: str) -> dict:
    m = re.search(r'<script id="bfm-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "embedded data block missing"
    return json.loads(m.group(1).replace("<\\/", "</"))


def _touching_pair(lab: np.ndarray, candidates: list[int]) -> tuple[int, int]:
    for a in candidates:
        ring = ndi.binary_dilation(lab == a, np.ones((3, 3), bool)) & (lab != a) & (lab > 0)
        for b in np.unique(lab[ring]):
            if int(b) in candidates:
                return a, int(b)
    raise AssertionError("no touching pair of objects in the synthetic field")


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #
def test_label_png_roundtrip() -> None:
    rng = np.random.default_rng(1)
    lab = rng.integers(0, 3000, (37, 53)).astype(np.int32)
    lab[0, 0] = 65535
    assert np.array_equal(decode_label_png(encode_label_png(lab)), lab)
    with pytest.raises(ValueError):
        encode_label_png(np.full((2, 2), 70000))


def test_resize_labels_is_nearest_neighbour() -> None:
    lab = np.zeros((40, 60), np.int32)
    lab[10:30, 10:50] = 7
    small = resize_labels(lab, (30, 20))
    assert small.shape == (20, 30) and set(np.unique(small)) == {0, 7}


def test_crop_montage_panels_recovers_panels() -> None:
    rng = np.random.default_rng(2)
    panels = [[rng.integers(0, 255, (30, 40, 3), dtype=np.uint8) for _ in range(3)] for _ in range(2)]
    sheet = grid_sheet(panels, [["a\nb"] * 3] * 2, None, None)
    out = crop_montage_panels(sheet, 2, 3)
    for i in range(2):
        for j in range(3):
            assert np.array_equal(out[i][j], panels[i][j])


# --------------------------------------------------------------------------- #
# ui build
# --------------------------------------------------------------------------- #
def test_ui_build_single_self_contained_file(pipeline: Path, tmp_path: Path) -> None:
    out = tmp_path / "ui" / "review.html"
    assert main(["ui", "build", "-c", str(pipeline), "-o", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    # no external resources: nothing loaded by URL, no separate script / stylesheet files
    assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', html, re.I)
    assert not re.search(r"<script[^>]+src=", html, re.I) and "<link" not in html.lower()
    assert "@import" not in html and "url(http" not in html
    doc = _doc(html)
    assert doc["schema"] == "morphoagent-ui-data/1" and doc["live"] is False
    assert "/" not in doc["dataset_name"] and str(tmp_path) not in json.dumps(doc)  # no absolute paths embedded
    cfg = load_config(pipeline)
    fields = pd.read_csv(Path(cfg.output_dir) / "06_select" / "fields.csv")
    assert {f["image"] for f in doc["fields"]} == set(fields.image)
    for f in doc["fields"]:
        assert f["bg"].startswith("data:image/jpeg;base64,") and f["labels"].startswith("data:image/png;base64,")
        lab = tifffile.imread(Path(cfg.output_dir) / fields.set_index("image").loc[f["image"], "label_tif"])
        decoded = decode_label_png(base64.b64decode(f["labels"].split(",", 1)[1]))
        assert np.array_equal(decoded, resize_labels(lab, (f["width"], f["height"])))
        assert {o["label"] for o in f["objects"]} == set(np.unique(lab)) - {0}
    e = doc["enhance"]
    assert e["available"] and len(e["samples"]) == 2
    targets = {v["median_target"] for v in e["samples"][0]["variants"] if v["preset"] == "figure-default"}
    assert set(cfg.ui.median_target_grid) <= targets
    assert sum(v["is_current"] for v in e["samples"][0]["variants"]) == 1
    g = doc["segment"]
    assert g["available"] and len(g["samples"]) == 2
    assert len(g["samples"][0]["panels"]) == len(g["settings"])
    panel = base64.b64decode(g["samples"][0]["panels"][0]["uri"].split(",", 1)[1])
    assert Image.open(io.BytesIO(panel)).width == 120
    man = json.loads((out.parent / "review_manifest.json").read_text())
    assert man["counts_out"]["fields"] == len(fields)
    # never overwrite without --overwrite
    assert main(["ui", "build", "-c", str(pipeline), "-o", str(out)]) == 1
    assert main(["ui", "build", "-c", str(pipeline), "-o", str(out), "--overwrite"]) == 0


def test_static_files_are_offline() -> None:
    text = static_text("app.js") + static_text("app.css") + static_text("page.html")
    assert not re.search(r"https?://", text)


# --------------------------------------------------------------------------- #
# Curation export -> morphoagent curate
# --------------------------------------------------------------------------- #
def _edit_targets(cfg_path: Path) -> tuple[str, int, int, int, np.ndarray]:
    cfg = load_config(cfg_path)
    out = Path(cfg.output_dir)
    obj = pd.read_csv(out / "06_select" / "objects.csv")
    fields = pd.read_csv(out / "06_select" / "fields.csv").set_index("image")
    image = fields.index[0]
    lab = tifffile.imread(out / fields.loc[image, "label_tif"])
    kept = obj[(obj.image == image) & (obj.group == "kept")].label.astype(int).tolist()
    a, b = _touching_pair(lab, kept)
    other = next(lb for lb in kept if lb not in (a, b))
    return image, other, a, b, lab


def _apply_and_check(cfg_path: Path, csv_text: str, image: str, deleted: int, a: int, b: int, target: int,
                     lab0: np.ndarray, tmp_path: Path) -> None:
    csv = tmp_path / "curation.csv"
    csv.write_text(csv_text)
    assert main(["curate", "-c", str(cfg_path), "--edits", str(csv), "--yes"]) == 0
    out = Path(load_config(cfg_path).output_dir)
    cur = tifffile.imread(out / "08_curate" / "labels" / f"{image}_labels_curated.tif")
    assert not (cur == deleted).any()
    gone = a if target == b else b
    assert not (cur == gone).any()
    assert (cur == target).sum() >= (lab0 == a).sum() + (lab0 == b).sum()
    cells = pd.read_csv(out / "08_curate" / "cells.csv")
    before = pd.read_csv(out / "06_select" / "cells.csv")
    assert len(cells) == len(before) - 2


def test_documented_csv_format_roundtrips_through_curate(pipeline: Path, tmp_path: Path) -> None:
    image, deleted, a, b, lab0 = _edit_targets(pipeline)
    text = f"operation,image,labels,target\ndelete,{image},{deleted},\nmerge,{image},{min(a, b)};{max(a, b)},{max(a, b)}\n"
    _apply_and_check(pipeline, text, image, deleted, a, b, max(a, b), lab0, tmp_path)


def _core_js() -> str:
    js = static_text("app.js")
    a, b = js.index("/* BEGIN curation-core */"), js.index("/* END curation-core */")
    return js[a:b]


def _node(script: str) -> str:
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_export_roundtrips_through_curate(pipeline: Path, tmp_path: Path) -> None:
    image, deleted, a, b, lab0 = _edit_targets(pipeline)
    script = _core_js() + f"""
var C = BfmCore, S = C.newState('x'), fe = C.field(S, {json.dumps(image)});
C.toggleDelete(fe, {deleted}); C.toggleDelete(fe, 999); C.toggleDelete(fe, 999);   // toggled twice = no edit
C.merge(fe, {a}, {b});
if (C.toggleDelete(fe, {a}).ok) throw new Error('deleting a merged label must be refused');
C.setTarget(fe, 0, {max(a, b)});
process.stdout.write(C.toCSV(C.rows(S, [{json.dumps(image)}, 'unused'])));
"""
    text = _node(script)
    assert text.splitlines()[0] == "operation,image,labels,target"
    assert text.splitlines()[1:] == [f"delete,{image},{deleted},", f"merge,{image},{min(a, b)};{max(a, b)},{max(a, b)}"]
    _apply_and_check(pipeline, text, image, deleted, a, b, max(a, b), lab0, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_core_merge_rules_and_suggestions_do_not_edit() -> None:
    script = _core_js() + """
var C = BfmCore, S = C.newState('d'), fe = C.field(S, 'A');
var out = {};
C.merge(fe, 5, 3); C.merge(fe, 8, 9); C.merge(fe, 9, 5);           // two merges united
out.merges = fe.merges;
C.setSuggestions(S, 'A', [{label: 4, kind: 'not_a_cell', reason: '<b>x</b>'}, {label: 2, kind: 'bogus'},
                          {label: 7, kind: 'fragmented', with: [6]}]);
out.after_suggest = JSON.parse(JSON.stringify(S.fields));
out.sugg = S.suggestions.A;
C.acceptSuggestion(fe, S.suggestions.A[0]);
out.deleted = fe.deleted;
var imp = C.normalize({schema: 'morphoagent-ui-state/1',
                       fields: {A: {deleted: ['4', 0], merges: [{labels: [2], target: 2}]}, Z: {}}},
                      ['A'], 'd');
out.imp = imp.state.fields; out.warn = imp.warnings;
process.stdout.write(JSON.stringify(out));
"""
    o = json.loads(_node(script))
    assert o["merges"] == [{"labels": [3, 5, 8, 9], "target": 3, "auto": True}]
    assert o["after_suggest"] == {"A": {"deleted": [], "merges": o["merges"], "done": False}}  # suggestions never edit
    assert [s["label"] for s in o["sugg"]] == [4, 7] and o["sugg"][1]["with"] == [6]
    assert o["deleted"] == [4]
    assert o["imp"] == {"A": {"deleted": [4], "merges": [], "done": False}} and any("Z" in w for w in o["warn"])


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def test_loopback_detection_and_token_rule() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.10")
    assert resolve_token("127.0.0.1", "TOK", {}) is None
    with pytest.raises(UIServerError, match="TOK"):
        resolve_token("0.0.0.0", "TOK", {})
    with pytest.raises(UIServerError, match="too short"):
        resolve_token("0.0.0.0", "TOK", {"TOK": "short"})
    assert resolve_token("0.0.0.0", "TOK", {"TOK": "x" * 20}) == "x" * 20


def test_server_refuses_non_loopback_without_token(pipeline: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORPHOAGENT_UI_TOKEN", raising=False)
    cfg = load_config(pipeline)
    with pytest.raises(UIServerError, match="MORPHOAGENT_UI_TOKEN"):
        make_server(cfg, host="0.0.0.0", port=0)
    assert main(["ui", "serve", "-c", str(pipeline), "--host", "0.0.0.0", "--port", "0"]) == 1


@pytest.fixture
def server(pipeline: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-token-0123456789"
    monkeypatch.setenv("MORPHOAGENT_UI_TOKEN", token)
    text = pipeline.read_text() + "ui:\n  llm: {enabled: true, provider: mock, max_requests_per_minute: 2}\n"
    pipeline.write_text(text)
    cfg = load_config(pipeline)
    httpd, app = make_server(cfg, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, token, app
    httpd.shutdown()
    httpd.server_close()


def _req(url: str, token: str | None = None, data: bytes | None = None, headers: dict | None = None):
    h = dict(headers or {})
    if token:
        h["X-BFM-Token"] = token
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_server_api_and_atomic_save(server, monkeypatch: pytest.MonkeyPatch) -> None:
    base, token, app = server
    assert _req(base + "/")[0] == 401
    assert _req(base + "/?token=wrong")[0] == 401
    st, body, _ = _req(base + f"/?token={token}")
    assert st == 200 and b'"live":true' in body
    st, body, _ = _req(base + "/api/fields", token)
    fields = json.loads(body)["fields"]
    assert st == 200 and len(fields) == len(app.fields)
    assert _req(base + "/api/fields", token, headers={"Origin": "http://evil.example"})[0] == 403
    state = {"schema": "morphoagent-ui-state/1", "fields": {fields[0]["image"]: {"deleted": [3], "merges": [], "done": True}}}
    st, _, _ = _req(base + "/api/save", token, json.dumps(state).encode())
    assert st == 200 and json.loads(app.state_path.read_text()) == state
    assert _req(base + "/api/save", token, b'{"schema": "other"}')[0] == 400
    assert _req(base + "/api/save", token, json.dumps({**state, "fields": {"nope": {}}}).encode())[0] == 400
    # atomic: a failure between writing and renaming leaves the previous file intact and no temp file behind
    import morphoagent.pipeline.ui.llm as llm_mod

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(llm_mod.os, "replace", boom)
    st, _, _ = _req(base + "/api/save", token, json.dumps({**state, "saved_utc": "later"}).encode())
    assert st == 500
    monkeypatch.undo()
    assert json.loads(app.state_path.read_text()) == state
    assert [p.name for p in app.state_path.parent.iterdir() if p.name.endswith(".tmp")] == []
    st, _, _ = _req(base + "/api/state", token)
    assert st == 200
    # live enhancement
    req = {"image": fields[0]["image"], "preset": "figure-default", "params": {"median_target": 140}}
    st, body, hdr = _req(base + "/api/enhance", token, json.dumps(req).encode())
    assert st == 200 and body[:2] == b"\xff\xd8" and "edge_strength" in json.loads(hdr["X-BFM-Metrics"])
    assert _req(base + "/api/enhance", token, json.dumps({**req, "image": "../etc"}).encode())[0] == 400


def test_mock_assistant_suggestions_never_modify_state(server) -> None:
    base, token, app = server
    image = next(iter(app.fields))
    state = {"schema": "morphoagent-ui-state/1", "fields": {image: {"deleted": [], "merges": [], "done": False}}}
    assert _req(base + "/api/save", token, json.dumps(state).encode())[0] == 200
    saved = app.state_path.read_bytes()
    st, body, _ = _req(base + f"/api/suggest/{image}", token)
    out = json.loads(body)
    assert st == 200 and out["suggestions"] and out["cached"] is False
    labels = {o["label"] for o in app.fields[image]["objects"]}
    assert all(s["label"] in labels and s["kind"] in llm.KINDS for s in out["suggestions"])
    assert app.state_path.read_bytes() == saved  # suggestions never edit anything
    st, body, _ = _req(base + f"/api/suggest/{image}", token)
    assert json.loads(body)["cached"] is True  # cached responses do not count against the rate limit
    assert app.assistant.cache.path.is_file()
    assert _req(base + "/api/suggest/unknown", token)[0] == 404


def test_rate_limiter_and_response_validation() -> None:
    now = [0.0]
    rl = llm.RateLimiter(2, clock=lambda: now[0])
    rl.acquire()
    rl.acquire()
    with pytest.raises(llm.RateLimitError):
        rl.acquire()
    now[0] = 61.0
    rl.acquire()
    resp = llm.parse_json_text('Here:\n```json\n{"suggestions": [{"label": 3, "kind": "Not a cell", "reason": "dust"},'
                               ' {"label": 99, "kind": "merged"}, {"label": 4, "kind": "fragmented", "with": [5, 99, 4]},'
                               ' {"label": "x", "kind": "merged"}]}\n```')
    out = llm.validate_response(resp, {3, 4, 5})
    assert out == [{"label": 3, "kind": "not_a_cell", "with": [], "reason": "dust"},
                   {"label": 4, "kind": "fragmented", "with": [5], "reason": ""}]
    with pytest.raises(llm.LLMError):
        llm.validate_response({"answer": 1}, {1})


def test_assistant_disabled_and_custom_provider(tmp_path: Path) -> None:
    calls = []

    def provider(request, settings, api_key):
        calls.append(request)
        assert request["task"] == llm.TASK and request["image"]["mime_type"] == "image/jpeg"
        return {"suggestions": [{"label": 1, "kind": "merged", "reason": "two nuclei"}]}

    overlay = np.zeros((40, 80, 3), np.uint8)
    objs = [{"label": 1, "area_px": 100.0, "border_contact_px": 0, "group": "kept", "group_rule": "kept"}]
    off = llm.Assistant(LLMConfig(), tmp_path / "c.json", provider)
    with pytest.raises(llm.LLMError, match="disabled"):
        off.suggest("A", overlay, objs)
    on = llm.Assistant(LLMConfig(enabled=True, provider="custom", image_width_px=32), tmp_path / "c.json", provider)
    out, cached = on.suggest("A", overlay, objs)
    assert out[0]["kind"] == "merged" and not cached and calls[0]["image"]["width"] == 32
    assert on.suggest("A", overlay, objs)[1] is True and len(calls) == 1
    with pytest.raises(llm.LLMError):
        llm.get_provider("no-such-provider")
