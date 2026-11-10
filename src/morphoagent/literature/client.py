"""Literature look-ups for the agent: search, abstracts, methods text and citation checks.

:class:`LiteratureClient` wraps NCBI E-utilities (esearch, esummary, efetch)
and the Europe PMC REST API (search, fullTextXML). Every public method returns
a JSON-serialisable dict whose values come from the services' responses, so
an agent can quote them; nothing is inferred.

Credentials are read only from the configuration or the environment
(``literature.ncbi_email`` / ``$MORPHOAGENT_NCBI_EMAIL``,
``$MORPHOAGENT_NCBI_API_KEY``) and are sent only to NCBI.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from ..pipeline.config import LiteratureConfig
from . import parsers as P
from .http import CachedHttp, LiteratureError, NotFoundError, Opener, default_cache_dir

LOG = logging.getLogger("morphoagent")

NCBI = "ncbi"
EPMC = "europepmc"
NCBI_RPS_WITHOUT_KEY = 3.0
NCBI_RPS_WITH_KEY = 10.0
#: Candidate records compared when a citation is checked by title alone.
TITLE_SEARCH_CANDIDATES = 5

Source = Literal["pubmed", "europepmc"]


class LiteratureClient:
    """PubMed / Europe PMC client with rate limits, retries and a response cache.

    Parameters
    ----------
    settings : LiteratureConfig, optional
        The ``literature`` configuration section (defaults when omitted).
    opener, sleep : callable, optional
        Injected transport and sleep function (tests).
    """

    def __init__(self, settings: LiteratureConfig | None = None, opener: Opener | None = None,
                 sleep: Any = None) -> None:
        s = settings or LiteratureConfig()
        self.settings = s
        email = s.ncbi_email or os.environ.get(s.ncbi_email_env, "")
        api_key = os.environ.get(s.ncbi_api_key_env, "")
        cap = NCBI_RPS_WITH_KEY if api_key else NCBI_RPS_WITHOUT_KEY
        ncbi_rps = min(s.ncbi_requests_per_second or cap, cap)
        cred = {"tool": s.tool_name}
        if email:
            cred["email"] = email
        if api_key:
            cred["api_key"] = api_key
        cache_dir = Path(s.cache_dir) if s.cache_dir else default_cache_dir()
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = CachedHttp(
            services={NCBI: (s.eutils_url, ncbi_rps), EPMC: (s.europepmc_url, s.europepmc_requests_per_second)},
            credentials={NCBI: cred},
            cache_dir=cache_dir,
            cache_ttl_days=s.cache_ttl_days,
            offline=s.offline,
            timeout_s=s.timeout_s,
            max_retries=s.max_retries,
            backoff_s=s.backoff_s,
            opener=opener,
            **kwargs,
        )
        LOG.debug("literature client: NCBI %.1f req/s (api key %s, email %s), cache %s, offline %s", ncbi_rps,
                  "set" if api_key else "not set", "set" if email else "not set", cache_dir, s.offline)

    # ------------------------------------------------------------- low level
    def _ncbi(self, endpoint: str, params: dict[str, Any]) -> str:
        resp = self.http.get(NCBI, endpoint, params, retry_on=lambda r: P.is_ncbi_rate_limited(r.body))
        if resp.status != 200:
            raise LiteratureError(f"NCBI {endpoint} returned HTTP {resp.status}")
        return resp.body

    def _esearch(self, term: str, retmax: int) -> dict[str, Any]:
        body = self._ncbi("esearch.fcgi", {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json",
                                           "sort": "relevance"})
        return P.parse_esearch(body)

    def _esummary(self, pmids: list[str]) -> dict[str, dict[str, Any] | None]:
        if not pmids:
            return {}
        body = self._ncbi("esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
        return P.parse_esummary(body)

    def _epmc_search(self, query: str, page_size: int) -> dict[str, Any]:
        resp = self.http.get(EPMC, "search", {"query": query, "format": "json", "resultType": "lite",
                                              "pageSize": page_size})
        if resp.status != 200:
            raise LiteratureError(f"Europe PMC search returned HTTP {resp.status}")
        return P.parse_epmc_search(resp.body)

    def _pmids_for_doi(self, doi: str) -> tuple[list[str], str]:
        """PMIDs whose record carries this DOI (PubMed first, then Europe PMC) and the service that answered."""
        res = self._esearch(f"{doi}[doi]", 5)
        if res["ids"]:
            return res["ids"], "pubmed"
        hits = self._epmc_search(f'DOI:"{doi}"', 5)["results"]
        pmids = [h["pmid"] for h in hits if h["pmid"] and (h["doi"] or "") == doi]
        return pmids, "europepmc" if pmids else ""

    # ------------------------------------------------------------- public API
    def search_literature(self, query: str, max_results: int = 10, source: Source = "pubmed") -> dict[str, Any]:
        """Search PubMed (esearch + esummary, relevance order) or Europe PMC.

        Parameters
        ----------
        query : str
            Query in the syntax of the chosen service.
        max_results : int
            Records returned (1 .. ``literature.max_results``).
        source : {"pubmed", "europepmc"}
            Service to search.

        Returns
        -------
        dict
            ``{"query", "source", "n_total", "n_returned", "results": [{pmid, doi, title, journal, year,
            authors, ...}]}``; ``n_total`` is the service's hit count.
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("query is empty")
        cap = self.settings.max_results
        if not 1 <= int(max_results) <= cap:
            raise ValueError(f"max_results must be in 1..{cap} (literature.max_results)")
        n = int(max_results)
        if source == "pubmed":
            res = self._esearch(query, n)
            summaries = self._esummary(res["ids"])
            results = []
            for pmid in res["ids"]:
                rec = summaries.get(pmid)
                if rec is None:
                    continue
                results.append({k: rec[k] for k in ("pmid", "doi", "title", "journal", "year", "authors", "pmcid")})
            total = res["count"]
        elif source == "europepmc":
            res = self._epmc_search(query, n)
            results = [{k: r[k] for k in ("pmid", "doi", "title", "journal", "year", "authors", "pmcid",
                                          "open_access", "source", "source_id")} for r in res["results"][:n]]
            total = res["count"]
        else:
            raise ValueError(f"source must be 'pubmed' or 'europepmc', got {source!r}")
        return {"query": query, "source": source, "n_total": total, "n_returned": len(results), "results": results}

    def fetch_abstract(self, pmid: str | int) -> dict[str, Any]:
        """Title, abstract, journal, year, DOI and PMC id of one PubMed record (efetch XML).

        Raises
        ------
        NotFoundError
            PubMed has no record with this PMID.
        """
        pmid = P.normalize_pmid(pmid)
        body = self._ncbi("efetch.fcgi", {"db": "pubmed", "id": pmid, "retmode": "xml"})
        recs = P.parse_efetch_abstract(body)
        rec = recs.get(pmid)
        if rec is None:
            raise NotFoundError(f"PMID {pmid} was not found in PubMed")
        rec["has_abstract"] = bool(rec["abstract"])
        rec["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        return rec

    def fetch_methods_text(self, pmcid: str) -> dict[str, Any]:
        """Methods-section paragraphs of an open-access article from Europe PMC (best effort).

        Raises
        ------
        NotFoundError
            Europe PMC has no full text for this PMC id (not open access, or unknown).
        """
        pmcid = P.normalize_pmcid(pmcid)
        resp = self.http.get(EPMC, f"{pmcid}/fullTextXML", {})
        if resp.status != 200 or not resp.body.lstrip().startswith("<"):
            raise NotFoundError(f"Europe PMC has no open-access full text for {pmcid} (HTTP {resp.status})")
        parsed = P.parse_jats_methods(resp.body)
        paras = parsed["paragraphs"]
        cap = self.settings.methods_max_paragraphs
        out = [{"index": i, **p} for i, p in enumerate(paras[:cap])]
        notes = []
        if not parsed["sections"]:
            notes.append("no section titled or typed as methods was found in the full text")
        if len(paras) > cap:
            notes.append(f"truncated to {cap} of {len(paras)} paragraphs (literature.methods_max_paragraphs)")
        return {"pmcid": pmcid, "pmid": parsed["pmid"], "doi": parsed["doi"], "title": parsed["title"],
                "sections": parsed["sections"], "n_paragraphs": len(paras), "paragraphs": out, "notes": notes,
                "url": f"https://europepmc.org/article/PMC/{pmcid}"}

    def verify_citation(self, pmid: str | int | None = None, doi: str | None = None,
                        title: str | None = None) -> dict[str, Any]:
        """Check that a citation exists and that its identifiers and title belong to the same paper.

        Checks (each ``True``, ``False`` or ``None`` = not applicable):

        * ``pmid_exists`` - PubMed has a record for the PMID (esummary).
        * ``doi_exists`` - the DOI is attached to a PubMed / Europe PMC record.
        * ``pmid_doi_match`` - the PMID's record carries the DOI (or the DOI resolves to that PMID).
        * ``title_match`` - title similarity >= ``literature.title_match_threshold``.

        ``verified`` is true only when a record was found and no check failed.

        Returns
        -------
        dict
            ``{"verified", "pmid", "doi", "title", "journal", "year", "title_match_score", "checks",
            "doi_points_to", "notes", "query"}``. ``pmid`` / ``doi`` / ``title`` describe the record
            found (or the input when none was found); ``doi_points_to`` lists the records the DOI
            resolves to when it disagrees with the PMID.
        """
        if not any(v not in (None, "") for v in (pmid, doi, title)):
            raise ValueError("give at least one of pmid, doi, title")
        thr = self.settings.title_match_threshold
        q_pmid = P.normalize_pmid(pmid) if pmid not in (None, "") else None
        q_doi = P.normalize_doi(doi) if doi not in (None, "") else None
        q_title = (title or "").strip() or None
        checks: dict[str, bool | None] = {"pmid_exists": None, "doi_exists": None, "pmid_doi_match": None,
                                          "title_match": None}
        notes: list[str] = []
        record: dict[str, Any] | None = None
        doi_points_to: list[dict[str, Any]] = []
        score: float | None = None

        if q_pmid:
            record = self._esummary([q_pmid]).get(q_pmid)
            checks["pmid_exists"] = record is not None
            if record is None:
                notes.append(f"PMID {q_pmid} does not exist in PubMed (esummary: no document summary)")

        if q_doi:
            doi_pmids, via = self._pmids_for_doi(q_doi)
            checks["doi_exists"] = bool(doi_pmids)
            if not doi_pmids:
                notes.append(f"DOI {q_doi} was not found in PubMed or Europe PMC")
            if record is not None:
                rec_doi = record.get("doi")
                if rec_doi:
                    checks["pmid_doi_match"] = rec_doi == q_doi
                elif doi_pmids:
                    checks["pmid_doi_match"] = q_pmid in doi_pmids
                if checks["pmid_doi_match"] is False:
                    others = self._esummary([p for p in doi_pmids if p != q_pmid])
                    doi_points_to = [{k: r[k] for k in ("pmid", "doi", "title", "journal", "year")}
                                     for r in others.values() if r is not None]
                    notes.append(
                        f"PMID {q_pmid} and DOI {q_doi} point to different papers: PMID {q_pmid} is "
                        f"'{record['title']}' ({record['journal']} {record['year']}, doi {rec_doi or 'none'})"
                        + (f"; the DOI resolves to PMID {', '.join(doi_pmids)} via {via}" if doi_pmids else ""))
                elif checks["pmid_doi_match"] is None:
                    notes.append(f"PMID {q_pmid} has no DOI on record and the DOI did not resolve; pairing unchecked")
            elif q_pmid is not None and doi_pmids:
                doi_points_to = [{k: r[k] for k in ("pmid", "doi", "title", "journal", "year")}
                                 for r in self._esummary(doi_pmids).values() if r is not None]
                notes.append(f"the DOI resolves to PMID {', '.join(doi_pmids)} via {via}")
            elif q_pmid is None and doi_pmids:
                record = self._esummary(doi_pmids[:1]).get(doi_pmids[0])
                if len(doi_pmids) > 1:
                    notes.append(f"DOI resolves to {len(doi_pmids)} PMIDs ({', '.join(doi_pmids)}); the first was used")

        if q_title:
            if record is None and q_pmid is None and q_doi is None:
                words = P.normalize_title(q_title)
                hits = self._esearch(f"{words}[ti]", TITLE_SEARCH_CANDIDATES)["ids"] if words else []
                cands = [r for r in self._esummary(hits).values() if r is not None]
                if cands:
                    best = max(cands, key=lambda r: P.title_similarity(q_title, r["title"]))
                    if P.title_similarity(q_title, best["title"]) >= thr:
                        record = best
                    else:
                        score = P.title_similarity(q_title, best["title"])
                        notes.append(f"closest PubMed title (PMID {best['pmid']}, similarity {score}) is "
                                     f"'{best['title']}'")
                else:
                    notes.append("no PubMed record matched the title words")
            if record is not None:
                score = P.title_similarity(q_title, record["title"])
                checks["title_match"] = score >= thr
                if not checks["title_match"]:
                    notes.append(f"title similarity {score} < {thr}: record title is '{record['title']}'")
            elif score is not None:
                checks["title_match"] = False

        verified = record is not None and not any(v is False for v in checks.values())
        if verified:
            notes.insert(0, "all applicable checks passed")
        out_rec = record or {}
        return {
            "verified": verified,
            "pmid": out_rec.get("pmid", q_pmid),
            "doi": out_rec.get("doi", q_doi),
            "title": out_rec.get("title", q_title),
            "journal": out_rec.get("journal"),
            "year": out_rec.get("year"),
            "title_match_score": score,
            "checks": checks,
            "doi_points_to": doi_points_to,
            "notes": notes,
            "query": {"pmid": q_pmid, "doi": q_doi, "title": q_title},
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{out_rec['pmid']}/" if record else None,
        }
