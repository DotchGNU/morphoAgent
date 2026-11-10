# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-11

First public release.

### Pipeline engine (`morphoagent.pipeline`, CLI `morphoagent`)

* Ten-step, config-driven, resumable pipeline: load, enhance-preview, enhance, segment-tune,
  segment, select, review, curate, vampire, stats. Every step is a subcommand and `run --from N --to M`
  runs any range; a step never replaces existing results without `--overwrite`.
* YAML configuration backed by typed dataclasses (`morphoagent init-config` writes the full default).
* Every step writes `run_manifest.json` (parameters, package versions, counts in and out, UTC time,
  configuration hash); every plot is saved as a 300-dpi transparent PNG, an SVG and
  `source_<name>.csv` with the plotted coordinates.
* Enhancement presets `figure-default`, `tone-unsharp`, `clahe`, `none` (display and review only;
  measurements always use the raw image).
* Cellpose-SAM segmentation on the downscaled raw image, labels upscaled to full size, with
  full-resolution measurements; parameter-grid previews with object counts (step 4).
* Rule-based selection: largest piece per label, optional image trim, minimum area, frame-contact
  length; the count tree is recorded in the manifest.
* Field table (`select.field_table`): run step 6 on a listed set of fields and, per field, on a
  given label file (hand-curated masks or another segmentation tool); label files are checked
  before any output is written.
* Numbered review overlays and a 16:9 review deck; delete / merge curation from a CSV, with
  re-measurement of edited objects and the originals left untouched.
* VAMPIRE shape modes through an external VAMPIRE_open checkout run by a separate interpreter,
  with elbow analysis, aspect-ratio renumbering, mode morphometry and mean contours.
* Object-level statistics: summaries, ratios, Welch tests with Benjamini-Hochberg q values,
  Fisher tests of mode composition, box + jitter plots and mode-fraction bars.
* `measure.shape_grid: full | segmentation`: axis features can be measured on the segmentation grid.
* `stats.pixel_calibration`: exact pixel size from field width, image width and binning; step 10
  records `pixel_size_um_effective`.

### Review page (`morphoagent ui build` / `ui serve`)

* Single offline HTML page with tabs for choosing the enhancement preset, choosing the segmentation
  setting, reviewing masks (delete / merge by clicking, undo / redo, zoom / pan, keyboard shortcuts)
  and exporting `curation.csv` and YAML configuration snippets.
* Standard-library server with atomic autosave, live enhancement previews and a token rule for
  non-loopback hosts; an optional, disabled-by-default, vendor-neutral LLM assistant whose
  suggestions must be accepted one by one.
* Decisions tab: proposal cards with thumbnails, tables, references and a recommended badge;
  accept / reject / comment / snapshot; offline download and `morphoagent decision --import`.
  Absolute paths are not shown in served proposals.

### Researcher-in-the-loop records (`morphoagent.records`)

* Versioned JSON records `morphoagent-proposal/1` and `morphoagent-decision/1` (pydantic models,
  JSON Schema files in `docs/schemas`); a decision stores the hash of the proposal it answers.
* Every decision writes a snapshot folder (`view.png`, `view.svg`, `source.csv`, `state.json`,
  `note.json`) that is never overwritten.
* `morphoagent report` builds a 16:9 slide deck and a Markdown report, one slide per snapshot,
  numbered `<prefix>_report_<YYMMDD>_c<N>`; absolute paths are redacted by default
  (`agent.report.redact_paths`).

### MCP servers (`morphoagent.mcp`)

* `morphoagent-mcp` (stdio): stage tools `inspect_folder`, `preview_enhance`, `run_segment`,
  `cut_distributions`, `run_vampire` and record tools `propose`, `list_pending`, `get_decision`,
  `build_report`. Stage tools refuse to run while a proposal of an earlier stage is undecided
  (`agent.require_decisions`).
* `morphoagent-literature` (stdio): `search_literature` (PubMed / Europe PMC), `fetch_abstract`,
  `fetch_methods_text` (open-access methods paragraphs) and `verify_citation` (flags nonexistent
  PMIDs and PMID / DOI pairs that belong to different papers). Standard-library HTTPS with
  per-service rate limits, retries, timeouts and an on-disk cache.
* Repository-level agent instructions and stage skills (consult, contrast, segment-cut, cluster,
  literature, report), and `.mcp.json` registering both servers.

### Validation and project

* Reproduced a previously completed manual analysis of a 14-condition phase-contrast dataset
  (126 images) exactly: identical segmentation, the same 2,561 selected cells, identical shape-mode
  assignments and statistics.
* Test suite on synthetic images only (Cellpose replaced by a threshold stand-in; literature tests on
  recorded responses); GitHub Actions CI on Python 3.10-3.12.
* License GPL-3.0-only; `CITATION.cff`, contributing guide, code of conduct, issue and pull request
  templates.

[0.1.0]: https://github.com/DotchGNU/morphoAgent/releases/tag/v0.1.0
