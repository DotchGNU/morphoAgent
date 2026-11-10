# Literature tools

Back to the [README](../README.md#mcp-servers-and-tools).

`morphoagent-literature` is a second MCP server (stdio) for stage 4 (literature-based revision
suggestions). It searches PubMed and Europe PMC, returns abstracts and open-access methods text, and
checks citations. The logic is in `src/morphoagent/literature/` (no MCP import), so it can also be
used from Python:

```python
from morphoagent.literature import LiteratureClient
c = LiteratureClient()                       # defaults; or LiteratureClient(load_config(path).literature)
c.verify_citation(pmid="33424024", doi="10.1038/s41596-020-00432-x")
```

```bash
pip install -e ".[mcp]"
morphoagent-literature                       # defaults
morphoagent-literature --config cfg.yaml     # use the `literature` section of cfg.yaml
```

Without `--config`, the file named by `$MORPHOAGENT_CONFIG` is used if set, otherwise the defaults.

## Tools

| Tool | Arguments | Service calls | Returns |
|---|---|---|---|
| `search_literature` | `query`, `max_results` (10; at most `literature.max_results`), `source` (`pubmed` \| `europepmc`) | PubMed: esearch (relevance) + esummary. Europe PMC: `search` (`resultType=lite`) | `query`, `source`, `n_total` (service hit count), `n_returned`, `results`: `pmid`, `doi`, `title`, `journal`, `year`, `authors` (first 3 + "et al."), `pmcid` (+ `open_access`, `source`, `source_id` for Europe PMC; `pmid` is null for records outside PubMed such as preprints) |
| `fetch_abstract` | `pmid` | efetch (XML) | `pmid`, `doi`, `pmcid`, `title`, `abstract` (structured abstracts as `LABEL: text` paragraphs), `abstract_sections`, `journal`, `journal_abbrev`, `year`, `has_abstract`, `url`; a tool error when the PMID does not exist |
| `fetch_methods_text` | `pmcid` | Europe PMC `fullTextXML` | `pmcid`, `pmid`, `doi`, `title`, `sections`, `n_paragraphs`, `paragraphs` (`index`, `section` path, `text`), `notes`, `url`; a tool error when Europe PMC has no full text (not open access) |
| `verify_citation` | `pmid`, `doi`, `title` (at least one) | esummary, esearch `<doi>[doi]` (Europe PMC `DOI:"..."` as fallback), esearch `<title words>[ti]` for a title alone | `verified`, `pmid`, `doi`, `title`, `journal`, `year` (of the record found), `title_match_score`, `checks`, `doi_points_to`, `notes`, `query`, `url` |

### `verify_citation`

Each check is `true`, `false` or `null` (not applicable):

| Check | Passes when |
|---|---|
| `pmid_exists` | esummary returns a document summary for the PMID |
| `doi_exists` | the DOI is attached to a PubMed record (or, failing that, a Europe PMC record with a PMID) |
| `pmid_doi_match` | the PMID's record carries the DOI; if the record has no DOI, the DOI resolves to that PMID |
| `title_match` | title similarity >= `literature.title_match_threshold` (0.9) |

`verified` is true only when a record was found and no check is `false`. Title similarity is the
`difflib` ratio of the two titles after lower-casing, removing accents, markup and punctuation.
When the PMID and DOI disagree, `doi_points_to` lists the record(s) the DOI belongs to and `notes`
names both papers. A PMID that does not exist gives `pmid_exists: false`.

Measured on the live services (2026-11-07): PMID 33424024 + DOI 10.1038/s41596-020-00432-x ->
verified; PMID 33349649 (an unrelated paper) + the same DOI -> `pmid_doi_match: false`,
`doi_points_to` = PMID 33424024; PMID 99999999 -> `pmid_exists: false`.

### Methods text

Methods sections are the outermost `<sec>` elements of the article body whose `sec-type` contains
`method` or whose title matches "Methods", "Materials and methods", "Experimental procedures" or
"Online methods". Paragraphs directly inside those sections (and their subsections) are returned in
document order; figure and table captions are skipped. At most `literature.methods_max_paragraphs`
paragraphs are returned. Articles that are in PubMed Central but not in the open-access subset (for
example author manuscripts) usually have no full text from Europe PMC.

## Requests, limits, cache

* HTTPS only, standard library (`urllib`); no extra dependency.
* NCBI parameters: `tool` = `literature.tool_name` (`morphoagent`) on every request; `email` only if
  `literature.ncbi_email` or `$MORPHOAGENT_NCBI_EMAIL` is set; `api_key` only if
  `$MORPHOAGENT_NCBI_API_KEY` is set. Nothing identifying is sent by default, and these values are
  never sent to Europe PMC.
* Rate limits: NCBI at most 3 requests/s without an API key and 10 with one (lower with
  `literature.ncbi_requests_per_second`); Europe PMC `literature.europepmc_requests_per_second` (5).
  The limit is the minimum interval between request starts, shared by all threads of the process.
* Timeout `literature.timeout_s` (20 s) per request; HTTP 429 / 500 / 502 / 503 / 504, timeouts and
  connection errors are retried `literature.max_retries` (3) times after `backoff_s x 2^attempt`
  seconds (or the `Retry-After` header, at most 30 s). An NCBI reply reporting an exceeded rate
  limit is retried the same way.
* Cache: one JSON file per request in `literature.cache_dir` (default: the user cache folder,
  `~/.cache/morphoagent/literature` on Linux, `~/Library/Caches/morphoagent/literature` on macOS,
  `%LOCALAPPDATA%\morphoagent\cache\literature` on Windows). The file name and the stored request
  exclude `tool`, `email` and `api_key`. Responses with status 200 and 404 are cached; entries older
  than `literature.cache_ttl_days` (30) are fetched again. `literature.offline: true` answers from
  the cache only and fails on a miss.

## Tests

`tests/test_literature.py` never touches the network (a fixture replaces `urllib.request.urlopen`).
It reads recorded responses from `tests/fixtures/literature/` in offline mode and compares every
result with `expected_results.json`, the output of the live recording. The fixtures are cache
entries reduced to the fields the parsers read (no affiliations, grants, reference lists, or
full-text sections other than methods; Europe PMC author lists cut to four names).
`tests/fixtures/literature/record_fixtures.py` re-records them from the live services. Retries,
`Retry-After`, the rate limiter, credential handling and the cache are tested with a fake transport.
