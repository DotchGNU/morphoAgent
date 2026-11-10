"""Re-record the literature fixtures from the live services (developer tool; not run by pytest).

Usage::

    python tests/fixtures/literature/record_fixtures.py            # prints every result as JSON

Each call below runs once against PubMed / Europe PMC with an empty temporary
cache. The cache entries are then reduced to the fields the parsers read
(author affiliations, grants, reference lists and full-text sections other
than methods are dropped) and written into this folder, where the tests read
them in offline mode. Credentials are never part of a cache entry.
"""

from __future__ import annotations

import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from morphoagent.literature import LiteratureClient, LiteratureError
from morphoagent.literature.parsers import _is_methods_sec
from morphoagent.pipeline.config import LiteratureConfig

HERE = Path(__file__).resolve().parent

QUERY = "VAMPIRE cell morphology Phillip 2021 Nature Protocols"
PMID = "33424024"
DOI = "10.1038/s41596-020-00432-x"
TITLE = "A robust unsupervised machine-learning method to quantify the morphological heterogeneity of cells and nuclei"
OTHER_PMID = "33349649"          # an existing PMID of an unrelated paper
MISSING_PMID = "99999999"        # no PubMed record
MISSING_DOI = "10.9999/morphoagent-nonexistent-0001"
OA_PMCID = "PMC9636021"          # open-access article with a Methods section
CLOSED_PMCID = "PMC8167883"      # in PMC, but no full text from Europe PMC

CALLS: list[tuple[str, str, dict[str, Any]]] = [
    ("search_pubmed", "search_literature", {"query": QUERY, "max_results": 10, "source": "pubmed"}),
    ("search_europepmc", "search_literature", {"query": QUERY, "max_results": 10, "source": "europepmc"}),
    ("abstract", "fetch_abstract", {"pmid": PMID}),
    ("abstract_missing", "fetch_abstract", {"pmid": MISSING_PMID}),
    ("verify_pair", "verify_citation", {"pmid": PMID, "doi": DOI}),
    ("verify_mismatch", "verify_citation", {"pmid": OTHER_PMID, "doi": DOI}),
    ("verify_missing_pmid", "verify_citation", {"pmid": MISSING_PMID}),
    ("verify_missing_pmid_with_doi", "verify_citation", {"pmid": MISSING_PMID, "doi": DOI}),
    ("verify_doi_only", "verify_citation", {"doi": DOI}),
    ("verify_title_only", "verify_citation", {"title": TITLE}),
    ("verify_wrong_title", "verify_citation", {"pmid": PMID, "title": "Goats work for food in a contrafreeloading task"}),
    ("verify_missing_doi", "verify_citation", {"doi": MISSING_DOI}),
    ("methods", "fetch_methods_text", {"pmcid": OA_PMCID}),
    ("methods_closed", "fetch_methods_text", {"pmcid": CLOSED_PMCID}),
]

ESUMMARY_KEYS = ("uid", "error", "title", "source", "fulljournalname", "pubdate", "epubdate", "authors", "articleids")
EPMC_KEYS = ("id", "source", "pmid", "pmcid", "doi", "title", "authorString", "journalTitle", "pubYear",
             "isOpenAccess", "bookOrReportDetails")
EFETCH_DROP = ("AffiliationInfo", "GrantList", "ReferenceList", "MeshHeadingList", "ChemicalList", "KeywordList",
               "CommentsCorrectionsList", "History", "Identifier", "SupplMeshList", "OtherAbstract")


def _drop(root: ET.Element, tags: tuple[str, ...]) -> None:
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in tags:
                parent.remove(child)


def sanitize(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only what the parsers read."""
    body, ep = entry["body"], entry["endpoint"]
    if entry["status"] != 200:
        return entry
    if ep == "esummary.fcgi":
        data = json.loads(body)
        res = data.get("result", {})
        for uid in res.get("uids", []):
            rec = {k: v for k, v in res[uid].items() if k in ESUMMARY_KEYS}
            if "authors" in rec:
                rec["authors"] = [{"name": a.get("name", "")} for a in rec["authors"]]
            if "articleids" in rec:
                rec["articleids"] = [{"idtype": a["idtype"], "value": a["value"]} for a in rec["articleids"]
                                     if a.get("idtype") in ("pubmed", "doi", "pmc")]
            res[uid] = rec
        body = json.dumps({"header": data.get("header"), "result": res}, ensure_ascii=False)
    elif ep == "search":
        data = json.loads(body)
        results = data.get("resultList", {}).get("result", [])
        data["resultList"]["result"] = [{k: r[k] for k in EPMC_KEYS if k in r} for r in results]
        for r in data["resultList"]["result"]:  # the parser reports the first 3 names + et al.; 4 are enough
            names = [x.strip() for x in r.get("authorString", "").rstrip(".").split(",") if x.strip()]
            if len(names) > 4:
                r["authorString"] = ", ".join(names[:4]) + "."
            if "bookOrReportDetails" in r:  # preprints: the parser reads only the publisher
                r["bookOrReportDetails"] = {"publisher": r["bookOrReportDetails"].get("publisher", "")}
        body = json.dumps({"hitCount": data.get("hitCount"), "resultList": data["resultList"]}, ensure_ascii=False)
    elif ep == "efetch.fcgi":
        root = ET.fromstring(body.encode("utf-8"))
        _drop(root, EFETCH_DROP)
        body = ET.tostring(root, encoding="unicode")
    elif ep.endswith("fullTextXML"):
        root = ET.fromstring(body.encode("utf-8"))
        front = root.find("front")
        meta = front.find("article-meta") if front is not None else None
        new = ET.Element("article")
        nfront = ET.SubElement(ET.SubElement(new, "front"), "article-meta")
        if meta is not None:
            for el in meta.findall("article-id") + meta.findall("title-group"):
                nfront.append(el)
        nbody = ET.SubElement(new, "body")
        b = root.find("body")

        def keep(src: ET.Element, dst: ET.Element) -> None:
            for sec in src.findall("sec"):
                if _is_methods_sec(sec):
                    dst.append(sec)
                else:  # keep the title only, so the outermost-section rule is still exercised
                    stub = ET.SubElement(dst, "sec", dict(sec.attrib))
                    t = sec.find("title")
                    if t is not None:
                        stub.append(t)
                    keep(sec, stub)

        if b is not None:
            keep(b, nbody)
        body = ET.tostring(new, encoding="unicode")
    return {**entry, "body": body}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        client = LiteratureClient(LiteratureConfig(cache_dir=tmp))
        results: dict[str, Any] = {}
        for name, method, kwargs in CALLS:
            try:
                results[name] = getattr(client, method)(**kwargs)
            except (LiteratureError, ValueError) as exc:
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
        for f in sorted(Path(tmp).glob("*.json")):
            entry = sanitize(json.loads(f.read_text(encoding="utf-8")))
            (HERE / f.name).write_text(json.dumps(entry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (HERE / "expected_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n",
                                                    encoding="utf-8")
        json.dump({"network_requests": client.http.n_network, "cache_hits": client.http.n_cache_hits,
                   "results": results}, sys.stdout, ensure_ascii=False, indent=1)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
