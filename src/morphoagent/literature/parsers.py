"""Pure parsers of PubMed E-utilities and Europe PMC responses (no network access).

Every function takes a response body (JSON or XML text) and returns plain
Python values, so each can be tested on recorded responses.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any

from .http import LiteratureError

_YEAR = re.compile(r"(1[89]\d\d|20\d\d|21\d\d)")
_DOI_PREFIX = re.compile(r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_PMCID = re.compile(r"^(?:PMC)?(\d{1,9})$", re.I)
_TAG = re.compile(r"<[^>]+>")
_METHODS_TITLE = re.compile(
    r"\b(methods?|materials\s+and\s+methods|methods\s+and\s+materials|experimental\s+procedures|online\s+methods)\b", re.I)


# --------------------------------------------------------------------------- #
# Identifiers and text
# --------------------------------------------------------------------------- #
def normalize_pmid(pmid: str | int) -> str:
    """Digits of a PMID (``'PMID: 123'``, ``123`` -> ``'123'``); ``ValueError`` otherwise."""
    s = re.sub(r"^\s*pmid\s*:?\s*", "", str(pmid), flags=re.I).strip()
    if not s.isdigit() or not 1 <= len(s) <= 9 or int(s) == 0:
        raise ValueError(f"not a PMID: {pmid!r} (expected 1-9 digits)")
    return str(int(s))


def normalize_doi(doi: str) -> str:
    """Lower-case DOI without ``doi:`` / ``https://doi.org/`` prefix or trailing punctuation; ``ValueError`` otherwise."""
    s = _DOI_PREFIX.sub("", str(doi)).strip().rstrip(".,;")
    if not _DOI.match(s):
        raise ValueError(f"not a DOI: {doi!r} (expected 10.<prefix>/<suffix>)")
    return s.lower()


def normalize_pmcid(pmcid: str | int) -> str:
    """``'PMC1234567'`` from ``'PMC1234567'``, ``'pmc1234567'`` or ``1234567``; ``ValueError`` otherwise."""
    m = _PMCID.match(str(pmcid).strip())
    if not m:
        raise ValueError(f"not a PMC id: {pmcid!r} (expected PMC followed by digits)")
    return f"PMC{int(m.group(1))}"


def normalize_title(title: str) -> str:
    """Lower case, accents removed, markup and punctuation replaced by spaces, whitespace collapsed."""
    s = html.unescape(_TAG.sub(" ", title or ""))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


def title_similarity(a: str, b: str) -> float:
    """Similarity of two titles in [0, 1] (difflib ratio of the normalised strings), 3 decimals."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return round(SequenceMatcher(None, na, nb, autojunk=False).ratio(), 3)


def short_authors(names: list[str], n: int = 3) -> str:
    """``'A, B, C, et al.'`` for more than ``n`` names, otherwise all names joined by commas."""
    names = [x.strip() for x in names if x and x.strip()]
    if len(names) > n:
        return ", ".join(names[:n]) + ", et al."
    return ", ".join(names)


def _year(text: str | None) -> int | None:
    m = _YEAR.search(text or "")
    return int(m.group(1)) if m else None


def _text(el: ET.Element | None) -> str:
    """All text inside an element, whitespace collapsed."""
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _json(body: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise LiteratureError(f"{what}: response is not JSON ({exc})") from None
    if not isinstance(data, dict):
        raise LiteratureError(f"{what}: unexpected JSON ({type(data).__name__})")
    return data


def _xml(body: str, what: str) -> ET.Element:
    try:
        return ET.fromstring(body.encode("utf-8"))
    except ET.ParseError as exc:
        raise LiteratureError(f"{what}: response is not XML ({exc})") from None


# --------------------------------------------------------------------------- #
# NCBI E-utilities
# --------------------------------------------------------------------------- #
def is_ncbi_rate_limited(body: str) -> bool:
    """True when an E-utilities 200 response reports an exceeded rate limit."""
    return "rate limit exceeded" in body[:500].lower()


def parse_esearch(body: str) -> dict[str, Any]:
    """``{"count": int, "ids": [pmid, ...], "query_translation": str}`` from esearch JSON."""
    data = _json(body, "esearch")
    res = data.get("esearchresult")
    if not isinstance(res, dict):
        raise LiteratureError(f"esearch: unexpected response {str(data)[:200]}")
    if "ERROR" in res:
        raise LiteratureError(f"esearch error: {res['ERROR']}")
    return {"count": int(res.get("count", 0) or 0), "ids": [str(i) for i in res.get("idlist", [])],
            "query_translation": res.get("querytranslation", "")}


def parse_esummary(body: str) -> dict[str, dict[str, Any] | None]:
    """``pmid -> record`` from esummary JSON; ``None`` for ids PubMed does not know.

    A record has ``pmid``, ``doi``, ``pmcid``, ``title``, ``journal``, ``journal_abbrev``,
    ``year``, ``authors`` (first 3 + et al.) and ``n_authors``.
    """
    data = _json(body, "esummary")
    if "error" in data and "result" not in data:
        raise LiteratureError(f"esummary error: {data['error']}")
    result = data.get("result") or {}
    out: dict[str, dict[str, Any] | None] = {}
    for uid in result.get("uids", []):
        rec = result.get(uid) or {}
        if not rec or "error" in rec:
            out[str(uid)] = None
            continue
        ids = {a.get("idtype"): a.get("value") for a in rec.get("articleids", []) if isinstance(a, dict)}
        names = [a.get("name", "") for a in rec.get("authors", []) if isinstance(a, dict)]
        doi = ids.get("doi")
        out[str(uid)] = {
            "pmid": str(uid),
            "doi": doi.lower() if doi else None,
            "pmcid": ids.get("pmc") or None,
            "title": html.unescape(rec.get("title", "")),
            "journal": rec.get("fulljournalname") or rec.get("source") or "",
            "journal_abbrev": rec.get("source", ""),
            "year": _year(rec.get("pubdate")) or _year(rec.get("epubdate")),
            "authors": short_authors(names),
            "n_authors": len(names),
        }
    return out


def parse_efetch_abstract(body: str) -> dict[str, dict[str, Any]]:
    """``pmid -> record`` with ``title``, ``abstract``, ``abstract_sections``, ``journal``, ``year``, ``doi``, ``pmcid``.

    Structured abstracts keep their labels: ``abstract`` joins ``"LABEL: text"``
    paragraphs with blank lines; ``abstract_sections`` lists ``{"label", "text"}``.
    """
    root = _xml(body, "efetch")
    out: dict[str, dict[str, Any]] = {}
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        if cit is None:
            continue
        pmid = _text(cit.find("PMID"))
        a = cit.find("Article")
        if a is None or not pmid:
            continue
        sections = []
        for at in a.findall("Abstract/AbstractText"):
            txt = _text(at)
            if txt:
                sections.append({"label": at.get("Label") or "", "text": txt})
        abstract = "\n\n".join(f"{s['label']}: {s['text']}" if s["label"] else s["text"] for s in sections)
        ids = {i.get("IdType"): (i.text or "").strip() for i in art.findall("PubmedData/ArticleIdList/ArticleId")}
        doi = ids.get("doi")
        if not doi:
            for e in a.findall("ELocationID"):
                if e.get("EIdType") == "doi" and e.text:
                    doi = e.text.strip()
        pub = a.find("Journal/JournalIssue/PubDate")
        year = _year(_text(pub.find("Year"))) if pub is not None else None
        if year is None and pub is not None:
            year = _year(_text(pub.find("MedlineDate")))
        if year is None:
            year = _year(_text(a.find("ArticleDate/Year")))
        out[pmid] = {
            "pmid": pmid,
            "doi": doi.lower() if doi else None,
            "pmcid": ids.get("pmc") or None,
            "title": _text(a.find("ArticleTitle")),
            "abstract": abstract,
            "abstract_sections": sections,
            "journal": _text(a.find("Journal/Title")),
            "journal_abbrev": _text(a.find("Journal/ISOAbbreviation")),
            "year": year,
        }
    return out


# --------------------------------------------------------------------------- #
# Europe PMC
# --------------------------------------------------------------------------- #
def parse_epmc_search(body: str) -> dict[str, Any]:
    """``{"count": int, "results": [record, ...]}`` from a Europe PMC ``search`` JSON response.

    A record has ``pmid`` (None for records outside PubMed, e.g. preprints),
    ``pmcid``, ``doi``, ``title``, ``journal``, ``year``, ``authors`` (first 3 +
    et al.), ``source``, ``source_id``, ``open_access``.
    """
    data = _json(body, "Europe PMC search")
    if "resultList" not in data:
        raise LiteratureError(f"Europe PMC search: unexpected response {str(data)[:200]}")
    results = []
    for r in data.get("resultList", {}).get("result", []) or []:
        names = [x.strip() for x in (r.get("authorString") or "").rstrip(".").split(",")]
        doi = r.get("doi")
        results.append({
            "pmid": r.get("pmid") or None,
            "pmcid": r.get("pmcid") or None,
            "doi": doi.lower() if doi else None,
            "title": html.unescape(_TAG.sub("", r.get("title") or "")).strip(),
            "journal": r.get("journalTitle") or r.get("bookOrReportDetails", {}).get("publisher", "") or "",
            "year": _year(r.get("pubYear")),
            "authors": short_authors(names),
            "source": r.get("source", ""),
            "source_id": r.get("id", ""),
            "open_access": r.get("isOpenAccess") == "Y",
        })
    return {"count": int(data.get("hitCount", 0) or 0), "results": results}


def _is_methods_sec(sec: ET.Element) -> bool:
    if "method" in (sec.get("sec-type") or "").lower():
        return True
    return bool(_METHODS_TITLE.search(_text(sec.find("title"))))


def _walk_methods(sec: ET.Element, path: list[str], out: list[dict[str, Any]]) -> None:
    for child in sec:
        if child.tag == "p":
            txt = _text(child)
            if txt:
                out.append({"section": " > ".join(path), "text": txt})
        elif child.tag == "sec":
            _walk_methods(child, [*path, _text(child.find("title")) or "(untitled)"], out)


def parse_jats_methods(body: str) -> dict[str, Any]:
    """Methods paragraphs of a JATS full-text XML (Europe PMC ``fullTextXML``).

    Methods sections are the outermost ``<sec>`` elements of ``<body>`` whose
    ``sec-type`` contains ``method`` or whose title matches "Methods",
    "Materials and methods", "Experimental procedures" or "Online methods".
    Paragraphs (``<p>`` directly inside a section; figure and table captions are
    skipped) are returned in document order with their section path.

    Returns
    -------
    dict
        ``{"title", "doi", "pmid", "sections": [title, ...], "paragraphs": [{"section", "text"}, ...]}``.
    """
    root = _xml(body, "full text")
    meta = root.find(".//front/article-meta")
    ids: dict[str, str] = {}
    title = ""
    if meta is not None:
        ids = {i.get("pub-id-type", ""): (i.text or "").strip() for i in meta.findall("article-id")}
        title = _text(meta.find("title-group/article-title"))
    body_el = root.find(".//body")
    paragraphs: list[dict[str, Any]] = []
    sections: list[str] = []

    def visit(el: ET.Element) -> None:
        for sec in el.findall("sec"):
            if _is_methods_sec(sec):
                name = _text(sec.find("title")) or (sec.get("sec-type") or "Methods")
                sections.append(name)
                _walk_methods(sec, [name], paragraphs)
            else:
                visit(sec)

    if body_el is not None:
        visit(body_el)
    doi = ids.get("doi")
    return {"title": title, "doi": doi.lower() if doi else None, "pmid": ids.get("pmid") or None,
            "sections": sections, "paragraphs": paragraphs}
