# Researcher-in-the-loop protocol: proposals, decisions, snapshots, reports

Where this fits in the overall design: [README](../README.md#decision-gates-snapshots-reports) (architecture figure).

The agent runs a stage and **proposes** candidate options; the researcher **decides** on the review
page; every decision writes a **snapshot** of the figure behind it; `morphoagent report` turns the
snapshots into a slide deck and a Markdown report. Later stage tools refuse to run while an earlier
proposal is undecided.

```
agent : stage tool -> candidates -> propose(...)              -> proposals/<id>.json (+ figure copies)
page  : Decisions tab -> Accept option / Reject + comment      -> decisions/<id>.json + snapshots/<...>/
agent : get_decision(<id>) -> edit the config -> next stage    (stage tools check the guard first)
any   : morphoagent report --out DIR                           -> <prefix>_report_<YYMMDD>_c<N>.pptx / .md
```

Code: `src/morphoagent/records/` (`protocol.py`, `snapshots.py`, `report.py`). Machine-readable
schemas: [schemas/proposal-1.schema.json](schemas/proposal-1.schema.json) and
[schemas/decision-1.schema.json](schemas/decision-1.schema.json) (JSON Schema generated from the
models; a test checks that they match).

## Records folder

`agent.records_dir`, default `<output_dir>/<agent.output_subdir>/records` (`<output_dir>/agent/records`).

```
records/
├── proposals/
│   ├── cut_20261107T101500Z_3fa2c1.json          proposal (written once)
│   └── cut_20261107T101500Z_3fa2c1_files/        copies of the referenced figures
│       ├── A__cut_histograms.png
│       ├── A__cut_histograms.svg                 copied when <stem>.svg exists next to the figure
│       └── source_A__cut_histograms.csv          copied when source_<stem>.csv exists next to it
├── decisions/
│   └── cut_20261107T101500Z_3fa2c1.json          decision (written once)
├── snapshots/
│   └── 20261107T101712Z_cut_minimum-cell-area/   view.png view.svg source.csv state.json note.json
└── run_manifest.json                             counts and an append-only event list
```

* **Proposal id**: `<stage>_<YYYYMMDDTHHMMSSZ>_<6 hex digits>` (UTC creation time, random token).
* **Never overwritten**: proposal and decision files are created with exclusive-create semantics
  (an existing file is an error); snapshot folders are created with `mkdir` that fails on an existing
  folder, and a name clash gets a `-2`, `-3`, ... suffix. Figures are copied at proposal time, so
  re-running a tool with `overwrite` cannot change what the researcher saw.
* `run_manifest.json`: `tool: "records"`, versions, config hash, `counts_out` (`proposals`,
  `decisions`, `pending`, `snapshots`) and `events` (`propose`, `decide`, `snapshot` with time and
  ids), updated after every write.

## Stages

| Stage | Pipeline | Tools whose guard checks earlier stages |
|---|---|---|
| `consult` | 0 consultation (`inspect_folder`) | — |
| `contrast` | 1 display enhancement | `preview_enhance` |
| `segment` | 2 Cellpose setting | `run_segment` |
| `cut` | 2 area / frame-contact cut-offs | `cut_distributions` |
| `cluster` | 3 VAMPIRE K | `run_vampire` |
| `literature` | 4 literature-based suggestions | — |

**Guard** (`agent.require_decisions: true`, default): a stage tool raises
`DecisionRequiredError` — returned to an MCP client as a tool error — when any proposal of an
**earlier** stage has no decision. Proposals of the same or a later stage do not block. A rejected
proposal counts as decided; the agent then writes a new proposal, which blocks again until decided.
The check runs before the tool writes anything. Python: `morphoagent.records.require_decision(cfg, stage)`.

## Proposal — schema `morphoagent-proposal/1`

```json
{
 "schema": "morphoagent-proposal/1",
 "proposal_id": "cut_20261107T101500Z_3fa2c1",
 "stage": "cut",
 "title": "Minimum cell area",
 "created_utc": "2026-11-07T10:15:00+00:00",
 "rationale": "150 px^2 keeps 48 of 55 objects (cut_table.csv).",
 "recommended": "area150",
 "options": [
  {"id": "area150", "label": "area >= 150 px^2", "description": "",
   "figures": ["cut_20261107T101500Z_3fa2c1_files/area150__cut_histograms.png"],
   "figure_origins": ["/path/to/output/agent/cut-distributions_1a2b3c4d5e/cut_histograms.png"],
   "table": [{"condition": "CTRL", "n_kept": 24}, {"condition": "DRUG", "n_kept": 24}],
   "numbers": {"cells kept": 48, "objects in": 55},
   "references": ["PMID:12345678", "doi:10.1000/example"],
   "config_changes": {"select.area_min_px": 150}}
 ],
 "evidence": [{"text": "48 of 55 objects kept", "path": "cut_table.csv", "tool": "cut_distributions", "references": []}],
 "provenance": {"tool": "cut_distributions", "parameters": {"area_min_px": [100.0, 150.0, 300.0]},
                "run_dir": "/path/to/output/agent/cut-distributions_1a2b3c4d5e", "manifest": ".../run_manifest.json"},
 "main_figure": "cut_20261107T101500Z_3fa2c1_files/area150__cut_histograms.png",
 "figure_state": {},
 "morphoagent_version": "0.1.0"
}
```

| Key | Type | Rule |
|---|---|---|
| `options[].id` | string | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$`, unique, not `rejected` |
| `options[].label` | string | 1-200 characters |
| `options[].figures` | list of paths | input: PNG or JPEG files that exist; stored: paths relative to `proposals/` |
| `options[].table` | list of objects | rows of scalar values (string, number, boolean, null); at most 500 |
| `options[].numbers` | object | name -> scalar; the first `agent.report.max_number_columns` keys become Markdown table columns, the first `agent.report.max_table_columns` of them slide columns |
| `options[].references` | list of strings | `PMID:<digits>` or `doi:10.<prefix>/<suffix>`; also accepted and normalised: `PMID 123`, `pmid:123`, `10.1000/x`, `https://doi.org/10.1000/x`. Only the format is checked |
| `options[].config_changes` | object | dotted config keys and values the agent writes when the option is chosen (informational; the protocol never edits the config) |
| `recommended` | string or null | an option id |
| `evidence[]` | object or string | `text` (required), `path`, `tool`, `references`; a plain string becomes `text` |
| `provenance` | object | `tool`, `parameters`, `run_dir`, `manifest`. When the agent gives no `tool`, the `run_manifest.json` next to (or one level above) the first figure is read (`tool` or `name`, and `parameters`) |
| `main_figure` | string or null | figure used for snapshots that are not tied to an option; default: first figure of the recommended option, else the first figure of any option |
| `figure_state` | object | figure settings copied into each snapshot's `state.json` |

Unknown keys are refused everywhere (`additionalProperties: false`).

## Decision — schema `morphoagent-decision/1`

```json
{
 "schema": "morphoagent-decision/1",
 "proposal_id": "cut_20261107T101500Z_3fa2c1",
 "stage": "cut",
 "choice": "area150",
 "comment": "keeps most cells",
 "decided_utc": "2026-11-07T10:17:12+00:00",
 "proposal_sha256": "<SHA-256 of the proposal file the researcher saw>",
 "source": "review-ui",
 "snapshot": "snapshots/20261107T101712Z_cut_minimum-cell-area",
 "morphoagent_version": "0.1.0"
}
```

* `choice`: an option id or `"rejected"`.
* `proposal_sha256` must equal the hash of the proposal file; otherwise the decision is refused as
  stale (HTTP 409).
* `source`: `review-ui` (live page), `review-ui-download` (downloaded from the offline page and
  imported) or `api` (Python call).
* A decision is final: a second decision for the same proposal is refused (HTTP 409).

## Snapshots

Written automatically by every decision, and by the **Snapshot** buttons on a card (serve mode).

| File | Content |
|---|---|
| `view.png` | the figure (first figure of the chosen / shown option, else `main_figure`), saved as PNG with `plot.dpi` (300) metadata; transparency kept |
| `view.svg` | the figure's `<stem>.svg` when it existed; for raster-only figures (contact sheets, montages) an SVG that embeds `view.png` at its print size (px / dpi) |
| `source.csv` | the figure's `source_<stem>.csv`; when the figure has none, the options table |
| `state.json` | schema `morphoagent-snapshot-state/1`: which file was used and how (`copied` / `png-embedded`), the option shown, dpi, `figure_state` |
| `note.json` | schema `morphoagent-snapshot-note/1`: `kind` (`decision` / `manual`), stage, proposal id and hash, title, shown option, choice and its label, comment, time, decision file, the full proposal, package versions |

A proposal without any figure is snapshotted as a table of its options drawn with matplotlib
(`view.png` 300 dpi transparent, `view.svg`), with the same table in `source.csv`. HTML is never
rasterised.

## Report

```bash
morphoagent report -c cfg.yaml --out reports/ [--prefix exp01]
```

* Snapshots are ordered by stage (table above), then time.
* `<prefix>_report_<YYMMDD>_c<N>.pptx` (16:9): one slide per snapshot with the title
  (`Stage n - name: title`), the decision line, `view.png`, a real table of the options
  (`mark` = chosen / recommended, `option`, `label`, the first `agent.report.max_number_columns`
  `numbers`; at most `agent.report.max_table_rows` rows, always including the chosen one), the
  comment, the agent's rationale, references as hyperlinks (PubMed / doi.org) and a provenance line
  (stage, tool, parameters, proposal id and hash, snapshot folder, time). A snapshot of a proposal without a figure (`state.json`
  `figure.file` is null) gets no picture; its table spans the slide width.
* `<prefix>_report_<YYMMDD>_c<N>.md`: the same content; figures are copied to
  `<prefix>_report_<YYMMDD>_c<N>_files/` and linked relatively.
* `N` is one more than the highest number present in the folder for that prefix, on any date
  (a report built the next day continues the numbering); nothing is overwritten. `<out>/run_manifest.json` lists every report built in the folder (file names,
  slide count, snapshot names, pptx hash).
* `<YYMMDD>` is the local date.

## Interfaces

| Action | MCP tool (`morphoagent-mcp`) | CLI | Review page (`ui serve`) | Python (`morphoagent.records`) |
|---|---|---|---|---|
| propose | `propose` | — | — | `propose(cfg, ...)` |
| list pending | `list_pending` | `morphoagent decision -c cfg --list` | Decisions tab | `list_pending(cfg)` |
| read a decision | `get_decision` (`decision` is null until decided) | — | — | `get_decision(cfg, id)` (None until decided) |
| decide | — (the agent never decides) | `morphoagent decision -c cfg --import FILE` (offline download) | Accept / Reject buttons -> `POST /api/decide` | `decide(cfg, id, choice, comment)` |
| snapshot | — | — | Snapshot buttons -> `POST /api/snapshot` | `manual_snapshot(cfg, id, option_id)` |
| report | `build_report` | `morphoagent report -c cfg --out DIR` | — | `records.report.build_report(cfg, out)` |

### HTTP endpoints (`morphoagent ui serve`)

Same token and origin rules as the other endpoints ([ui.md](ui.md#security-model)).

| Endpoint | Body | Response |
|---|---|---|
| `GET /api/proposals` | — | `{"proposals": [{"proposal", "sha256", "decision"}], "stages", "stage_titles", "require_decisions", "reference_urls"}`; each option carries `thumbs` (JPEG data URIs, `agent.thumbnail_width_px` wide); absolute paths are removed |
| `POST /api/decide` | `{"proposal_id", "choice", "comment", "proposal_sha256"}` | `{"proposal_id", "choice", "decided_utc", "snapshot"}`; 400 invalid choice, 404 unknown proposal, 409 already decided or stale hash |
| `POST /api/snapshot` | `{"proposal_id", "option_id" (optional), "comment"}` | `{"snapshot", "files"}` |

### Offline page

The page built by `morphoagent ui build` embeds the proposals that exist at build time. Accept /
Reject there downloads `<proposal_id>.json` (a complete decision document, `source:
"review-ui-download"`); `morphoagent decision -c cfg --import <file>` validates it, checks the hash,
writes the decision and its snapshot. Snapshot buttons need the server.
