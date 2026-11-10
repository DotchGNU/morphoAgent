"""Literature tools on recorded responses (no network) and on a fake transport."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from morphoagent.literature import CachedHttp, LiteratureClient, LiteratureError, NotFoundError
from morphoagent.literature import parsers as P
from morphoagent.literature.http import RateLimiter
from morphoagent.pipeline.config import ConfigError, LiteratureConfig, load_config

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "literature"
PMID = "33424024"
DOI = "10.1038/s41596-020-00432-x"
FABRICATED_PMID = "33349649"  # exists, but is a different paper than DOI
MISSING_PMID = "99999999"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def offline() -> LiteratureClient:
    return LiteratureClient(LiteratureConfig(cache_dir=str(FIXTURES), offline=True))


@pytest.fixture
def lit_config(tmp_path: Path) -> Path:
    p = tmp_path / "lit.yaml"
    p.write_text(yaml.safe_dump({"literature": {"cache_dir": str(FIXTURES), "offline": True}}))
    return p


# --------------------------------------------------------------------------- #
# Recorded responses
# --------------------------------------------------------------------------- #
def test_offline_results_equal_the_live_recording(offline: LiteratureClient) -> None:
    """Every recorded call gives the same result from the sanitised fixtures as it did live."""
    sys.path.insert(0, str(FIXTURES))
    try:
        import record_fixtures as rec
    finally:
        sys.path.remove(str(FIXTURES))
    expected = json.loads((FIXTURES / "expected_results.json").read_text(encoding="utf-8"))
    n = 0
    for name, method, kwargs in rec.CALLS:
        if "HTTP 500" in str(expected[name].get("error", "")):
            continue  # not cached by design (only 200 / 404 are)
        try:
            got: dict[str, Any] = getattr(offline, method)(**kwargs)
        except LiteratureError as exc:
            got = {"error": f"{type(exc).__name__}: {exc}"}
        assert got == expected[name], name
        n += 1
    assert n == len(rec.CALLS) - 1
    assert offline.http.n_network == 0


def test_search_pubmed(offline: LiteratureClient) -> None:
    res = offline.search_literature("VAMPIRE cell morphology Phillip 2021 Nature Protocols")
    assert res["n_returned"] == 1
    hit = res["results"][0]
    assert (hit["pmid"], hit["doi"], hit["year"]) == (PMID, DOI, 2021)
    assert hit["authors"] == "Phillip JM, Han KS, Chen WC, et al."
    assert set(hit) >= {"pmid", "doi", "title", "journal", "year", "authors"}


def test_search_europepmc_includes_records_outside_pubmed(offline: LiteratureClient) -> None:
    res = offline.search_literature("VAMPIRE cell morphology Phillip 2021 Nature Protocols", source="europepmc")
    assert any(r["pmid"] == PMID and r["doi"] == DOI for r in res["results"])
    assert any(r["pmid"] is None for r in res["results"])
    assert all(r["authors"].count(",") <= 3 for r in res["results"])


def test_fetch_abstract(offline: LiteratureClient) -> None:
    a = offline.fetch_abstract(f"PMID: {PMID}")
    assert a["pmid"] == PMID and a["doi"] == DOI and a["year"] == 2021 and a["pmcid"] == "PMC8167883"
    assert a["has_abstract"] and a["abstract"].startswith("Cell morphology encodes essential information")
    with pytest.raises(NotFoundError):
        offline.fetch_abstract(MISSING_PMID)


def test_verify_citation_correct_pair(offline: LiteratureClient) -> None:
    v = offline.verify_citation(pmid=PMID, doi=f"https://doi.org/{DOI.upper()}")
    assert v["verified"] is True
    assert v["checks"] == {"pmid_exists": True, "doi_exists": True, "pmid_doi_match": True, "title_match": None}


def test_verify_citation_flags_a_fabricated_pmid_doi_pair(offline: LiteratureClient) -> None:
    v = offline.verify_citation(pmid=FABRICATED_PMID, doi=DOI)
    assert v["verified"] is False
    assert v["checks"]["pmid_exists"] is True and v["checks"]["pmid_doi_match"] is False
    assert [r["pmid"] for r in v["doi_points_to"]] == [PMID]
    assert "point to different papers" in v["notes"][0]


def test_verify_citation_flags_a_nonexistent_pmid(offline: LiteratureClient) -> None:
    v = offline.verify_citation(pmid=MISSING_PMID)
    assert v["verified"] is False and v["checks"]["pmid_exists"] is False
    v2 = offline.verify_citation(pmid=MISSING_PMID, doi=DOI)
    assert v2["verified"] is False and [r["pmid"] for r in v2["doi_points_to"]] == [PMID]


def test_verify_citation_doi_title_and_missing_doi(offline: LiteratureClient) -> None:
    assert offline.verify_citation(doi=DOI)["pmid"] == PMID
    t = offline.verify_citation(title="A robust unsupervised machine-learning method to quantify the morphological "
                                      "heterogeneity of cells and nuclei")
    assert t["verified"] and t["pmid"] == PMID and t["title_match_score"] == 1.0
    w = offline.verify_citation(pmid=PMID, title="Goats work for food in a contrafreeloading task")
    assert not w["verified"] and w["checks"]["title_match"] is False and w["title_match_score"] < 0.9
    m = offline.verify_citation(doi="10.9999/morphoagent-nonexistent-0001")
    assert not m["verified"] and m["checks"]["doi_exists"] is False


def test_verify_citation_rejects_bad_input(offline: LiteratureClient) -> None:
    with pytest.raises(ValueError):
        offline.verify_citation()
    with pytest.raises(ValueError):
        offline.verify_citation(pmid="12a")
    with pytest.raises(ValueError):
        offline.verify_citation(doi="not-a-doi")


def test_fetch_methods_text(offline: LiteratureClient) -> None:
    m = offline.fetch_methods_text("pmc9636021")
    assert m["pmcid"] == "PMC9636021" and m["sections"] == ["Methods"]
    assert m["n_paragraphs"] == len(m["paragraphs"]) == 40
    assert all(p["section"].startswith("Methods > ") and p["text"] for p in m["paragraphs"])
    assert [p["index"] for p in m["paragraphs"]] == list(range(40))


def test_offline_cache_miss_is_an_error(offline: LiteratureClient) -> None:
    with pytest.raises(LiteratureError, match="offline mode"):
        offline.fetch_abstract("1")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_identifier_normalisation_and_titles() -> None:
    assert P.normalize_pmid(" pmid:0123 ") == "123"
    assert P.normalize_doi("doi: 10.1038/ABC.") == "10.1038/abc"
    assert P.normalize_pmcid("8167883") == "PMC8167883"
    for bad in ("", "0", "1234567890"):
        with pytest.raises(ValueError):
            P.normalize_pmid(bad)
    assert P.title_similarity("<i>A</i> Study of Cells.", "a study of cells") == 1.0
    assert P.title_similarity("abc", "") == 0.0
    assert P.short_authors(["A", "B", "C"]) == "A, B, C"
    assert P.short_authors(["A", "B", "C", "D"]) == "A, B, C, et al."


def test_jats_methods_outermost_and_captions_skipped() -> None:
    xml = ("<article><front><article-meta><article-id pub-id-type='doi'>10.1/X</article-id>"
           "<title-group><article-title>T</article-title></title-group></article-meta></front><body>"
           "<sec><title>Results</title><p>r</p></sec>"
           "<sec sec-type='materials|methods'><title>Materials and methods</title><p>m0</p>"
           "<fig><caption><p>caption</p></caption></fig>"
           "<sec><title>Cell culture</title><p>m1</p><sec sec-type='methods'><title>Sub</title><p>m2</p></sec></sec>"
           "</sec></body></article>")
    out = P.parse_jats_methods(xml)
    assert out["doi"] == "10.1/x" and out["sections"] == ["Materials and methods"]
    assert [(p["section"], p["text"]) for p in out["paragraphs"]] == [
        ("Materials and methods", "m0"),
        ("Materials and methods > Cell culture", "m1"),
        ("Materials and methods > Cell culture > Sub", "m2"),
    ]


def test_parsers_reject_garbage() -> None:
    with pytest.raises(LiteratureError):
        P.parse_esearch("<html>")
    with pytest.raises(LiteratureError):
        P.parse_efetch_abstract("{not xml")


# --------------------------------------------------------------------------- #
# Transport: retries, rate limit, cache, credentials
# --------------------------------------------------------------------------- #
class FakeOpener:
    def __init__(self, replies: list[tuple[int, bytes, dict[str, str]]]) -> None:
        self.replies = list(replies)
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Any, timeout_s: float) -> tuple[int, bytes, str, dict[str, str]]:
        self.urls.append(url)
        status, body, hdrs = self.replies.pop(0)
        return status, body, "application/json", hdrs


def _http(tmp_path: Path, opener: FakeOpener, sleeps: list[float], **kw: Any) -> CachedHttp:
    return CachedHttp({"svc": ("https://example.org/api", 1000.0)}, credentials={"svc": {"tool": "t", "email": "e@x"}},
                      cache_dir=tmp_path, opener=opener, sleep=sleeps.append, **kw)


def test_retries_with_backoff_and_retry_after(tmp_path: Path) -> None:
    sleeps: list[float] = []
    op = FakeOpener([(503, b"", {}), (429, b"", {"Retry-After": "7"}), (200, b'{"ok": 1}', {})])
    http = _http(tmp_path, op, sleeps, max_retries=3, backoff_s=0.5)
    r = http.get("svc", "e", {"q": "x"})
    assert r.status == 200 and r.body == '{"ok": 1}'
    assert [s for s in sleeps if s >= 0.5] == [0.5, 7.0]
    assert len(op.urls) == 3


def test_retries_exhausted_and_transport_error(tmp_path: Path) -> None:
    sleeps: list[float] = []
    http = _http(tmp_path, FakeOpener([(503, b"", {})] * 2), sleeps, max_retries=1)
    assert http.get("svc", "e", {}).status == 503  # final status returned, not cached

    def broken(*_a: Any) -> Any:
        raise TimeoutError("slow")

    http2 = CachedHttp({"svc": ("https://example.org", 1000.0)}, cache_dir=None, opener=broken, sleep=sleeps.append,
                       max_retries=2)
    with pytest.raises(LiteratureError, match="3 attempt"):
        http2.get("svc", "e", {})


def test_cache_has_no_credentials_and_is_reused(tmp_path: Path) -> None:
    sleeps: list[float] = []
    op = FakeOpener([(200, b"body", {}), (404, b"nf", {})])
    http = _http(tmp_path, op, sleeps)
    assert http.get("svc", "e", {"q": "1"}).body == "body"
    assert "email=e%40x" in op.urls[0] and "tool=t" in op.urls[0]
    again = http.get("svc", "e", {"q": "1", "email": "other"})
    assert again.from_cache and len(op.urls) == 1
    assert http.get("svc", "e", {"q": "2"}).status == 404
    assert http.get("svc", "e", {"q": "2"}).from_cache  # 404 is cached and not retried
    for f in tmp_path.glob("*.json"):
        text = f.read_text()
        assert "e@x" not in text and '"tool"' not in text
    expired = _http(tmp_path, FakeOpener([(200, b"new", {})]), sleeps, cache_ttl_days=0)
    assert expired.get("svc", "e", {"q": "1"}).body == "new"


def test_https_only_and_rate_limiter() -> None:
    with pytest.raises(ValueError, match="https"):
        CachedHttp({"svc": ("http://example.org", 1.0)})
    t = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    rl = RateLimiter(3.0, clock=lambda: t[0], sleep=sleep)
    for _ in range(4):
        rl.wait()
    assert len(slept) == 3 and all(abs(s - 1 / 3) < 1e-9 for s in slept)


def test_ncbi_credentials_from_env_and_rate_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MORPHOAGENT_NCBI_API_KEY", "k" * 10)
    monkeypatch.setenv("MORPHOAGENT_NCBI_EMAIL", "someone@example.org")
    op = FakeOpener([(200, json.dumps({"esearchresult": {"count": "0", "idlist": []}}).encode(), {})])
    c = LiteratureClient(LiteratureConfig(cache_dir=str(tmp_path)), opener=op, sleep=lambda s: None)
    assert c.http.limiters["ncbi"].interval_s == pytest.approx(0.1)
    c.search_literature("x")
    assert "api_key=kkkkkkkkkk" in op.urls[0] and "email=someone%40example.org" in op.urls[0]
    monkeypatch.delenv("MORPHOAGENT_NCBI_API_KEY")
    monkeypatch.delenv("MORPHOAGENT_NCBI_EMAIL")
    c2 = LiteratureClient(LiteratureConfig(cache_dir=str(tmp_path), ncbi_requests_per_second=10))
    assert c2.http.limiters["ncbi"].interval_s == pytest.approx(1 / 3)
    assert c2.http.credentials["ncbi"] == {"tool": "morphoagent"}


def test_rate_limit_body_is_retried(tmp_path: Path) -> None:
    op = FakeOpener([(200, b'{"error":"API rate limit exceeded"}', {}),
                     (200, json.dumps({"esearchresult": {"count": "0", "idlist": []}}).encode(), {})])
    c = LiteratureClient(LiteratureConfig(cache_dir=str(tmp_path)), opener=op, sleep=lambda s: None)
    assert c.search_literature("x")["n_returned"] == 0 and len(op.urls) == 2


def test_methods_not_available(tmp_path: Path) -> None:
    c = LiteratureClient(LiteratureConfig(cache_dir=str(tmp_path), max_retries=0),
                         opener=FakeOpener([(404, b"", {})]), sleep=lambda s: None)
    with pytest.raises(NotFoundError, match="no open-access full text"):
        c.fetch_methods_text("PMC1")


def test_literature_config_validation(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    for text in ("literature:\n  ncbi_requests_per_second: 20\n", "literature:\n  eutils_url: http://x\n",
                 "literature:\n  title_match_threshold: 0\n"):
        p.write_text(text)
        with pytest.raises(ConfigError):
            load_config(p)
    p.write_text("literature:\n  cache_dir: cache\n")
    assert load_config(p).literature.cache_dir == str((tmp_path / "cache").resolve())


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
def test_literature_server_tools_in_process(lit_config: Path) -> None:
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError

    from morphoagent.mcp import literature_server as ls

    ls.configure(str(lit_config))
    try:
        tools = {t.name: t for t in asyncio.run(ls.server.list_tools())}
        assert sorted(tools) == ["fetch_abstract", "fetch_methods_text", "search_literature", "verify_citation"]
        assert tools["search_literature"].input_schema["properties"]["source"]["enum"] == ["pubmed", "europepmc"]
        res = asyncio.run(ls.server.call_tool("verify_citation", {"pmid": FABRICATED_PMID, "doi": DOI}))
        data = res.structured_content if hasattr(res, "structured_content") else res
        assert data["verified"] is False
        with pytest.raises(ToolError, match="not a PMID"):
            asyncio.run(ls.server.call_tool("fetch_abstract", {"pmid": "abc"}))
    finally:
        ls.configure(None)


def test_both_servers_over_stdio(lit_config: Path) -> None:
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    env = {"PYTHONNOUSERSITE": "1"}
    pipe = StdioServerParameters(command=sys.executable, args=["-m", "morphoagent.mcp.pipeline_server"], env=env)
    lit = StdioServerParameters(command=sys.executable,
                                args=["-m", "morphoagent.mcp.literature_server", "--config", str(lit_config)], env=env)

    async def go() -> tuple[list[str], list[str], dict, bool]:
        async with Client(pipe) as c1:
            names1 = sorted(t.name for t in (await c1.list_tools()).tools)
        async with Client(lit) as c2:
            names2 = sorted(t.name for t in (await c2.list_tools()).tools)
            ok = await c2.call_tool("verify_citation", {"pmid": PMID, "doi": DOI})
            bad = await c2.call_tool("fetch_abstract", {"pmid": "12345"})  # not in the offline cache
        return names1, names2, ok.structured_content, bool(bad.is_error)

    names1, names2, ok, bad_is_error = asyncio.run(go())
    assert "propose" in names1 and "run_vampire" in names1
    assert names2 == ["fetch_abstract", "fetch_methods_text", "search_literature", "verify_citation"]
    assert ok["verified"] is True and ok["pmid"] == PMID
    assert bad_is_error
