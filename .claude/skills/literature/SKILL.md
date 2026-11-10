---
name: literature
description: Stage 4 literature-based revision suggestions. Use after the cluster decision, or when the researcher asks whether a choice (contrast preset, segmentation setting, cut-offs, K) has support in the literature - search PubMed / Europe PMC, verify every citation, quote sentences verbatim, and propose revisions with that evidence or state that no literature support was found.
---

# Stage 4 - literature-based revision suggestions

## When to use

Stages 0-3 are decided (check with `list_pending`). The question is narrow: should any decided
choice be revised, given published methods? Examples: the range of K used for VAMPIRE shape modes,
minimum cell size or border rules, how cells touching the image frame are handled.

## Tool calls (server `morphoagent-literature`)

1. `search_literature(query, max_results=10, source="pubmed")`; repeat with
   `source="europepmc"` for preprints and open-access full text. Use specific queries (method name
   + parameter + cell type). Record the queries you ran.
2. For each candidate: `fetch_abstract(pmid)`. If the abstract does not contain the parameter,
   and a `pmcid` is given, `fetch_methods_text(pmcid)` (open access only; a tool error means no full
   text - say so, do not guess the content).
3. `verify_citation(pmid=..., doi=..., title=...)` for every paper you will cite. Cite it only if
   `verified` is `true`. If verification fails (`pmid_exists: false`, `pmid_doi_match: false`,
   `title_match: false`), drop the paper and say why using its `notes`.
4. Quote the supporting sentence verbatim from the `abstract` or a methods `paragraphs[].text`,
   with the section name. Do not paraphrase a quote, and do not quote text you did not receive.

## Rules

* No sentence, no claim: if no verified sentence supports a revision, write
  "no literature support found" for that question.
* A published value from another cell type, magnification or imaging mode is context, not a rule;
  say how the paper's setting differs from this data set (from the quoted text only).
* Numbers from this analysis still come only from stage tool outputs of this session.

## The card

`propose(stage="literature", title="Literature check: <topic>", ...)`:

* Options: "keep current choice" plus one option per suggested revision. Each revision option has
  `references` = the verified identifiers (`PMID:<n>` and/or `doi:<...>`), `description` = the
  quoted sentence(s) with section names, `config_changes` = the keys it would change.
* `evidence` = one entry per quote (`text` = the quote, `tool` = `fetch_abstract` or
  `fetch_methods_text`, `references` = its identifier).
* `recommended` may be "keep current choice"; a revision is only a suggestion. If it is chosen,
  the affected stage is re-run after the researcher agrees (outputs go to new folders; ask before
  any `overwrite`).

## Stop conditions

* Stop after `propose`. Do not change the configuration or re-run a stage before the decision.
* Stop when the searches return no verified source; report the queries and "no literature support
  found".
