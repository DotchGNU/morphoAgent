# Agent tools (MCP server)

Overview, architecture figure and quick start with an agent client: [README](../README.md#quick-start-a-with-an-agent-client).

`morphoagent-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server (stdio
transport) that exposes pipeline stages 0-3 and the researcher-in-the-loop protocol
([protocol.md](protocol.md)) as typed tools. Any MCP client can use it. The tools
are thin wrappers of plain Python functions in `src/morphoagent/mcp/stage_tools.py`, which can also
be called directly (tests, notebooks).

```bash
pip install -e ".[mcp]"
morphoagent-mcp                                # or: python -m morphoagent.mcp.pipeline_server
```

`.mcp.json` at the repository root registers the server for clients that read that file:

```json
{"mcpServers": {"morphoagent-pipeline": {"type": "stdio", "command": "morphoagent-mcp", "args": [],
                                         "env": {"PYTHONNOUSERSITE": "1"}}}}
```

A second server, `morphoagent-literature`, provides the stage-4 literature tools
(`search_literature`, `fetch_abstract`, `fetch_methods_text`, `verify_citation`); it is registered in
the same `.mcp.json` and documented in [literature.md](literature.md).

`morphoagent-mcp` must be on the client's `PATH` (activate the environment before starting the
client, or replace `command` with the absolute path of the environment's `morphoagent-mcp`).

## Rules shared by all tools

* Input images are only read, never written.
* Existing results are never replaced unless the call passes `overwrite: true`; the pipeline then
  removes and recreates only the folder of that step.
* Tools 1-3 read the YAML configuration given in `config_path`. A decision (preset, Cellpose
  setting, cut-offs, K) is made by editing that file; tools never write to it.
* Each result is a JSON object with the paths of the files produced and summary numbers read back
  from those files.
* Stage tools 1-3 refuse to run while a proposal of an earlier stage waits for the researcher's
  decision (`agent.require_decisions`, see [protocol.md](protocol.md#stages)); the refusal is a tool
  error naming the pending proposals.
* Anticipated failures (missing input, existing output, invalid value, failed external program,
  undecided earlier proposal) are returned as tool errors with their message; other exceptions are reported as crashes without
  details and logged by the server on stderr.

## Tools

| Stage | Tool | Arguments | Writes | Returns |
|---|---|---|---|---|
| 0 | `inspect_folder` | `path`, `name_regex`, `strip_prefix`, `strip_suffix`, `extensions`, `read_pixels` | nothing | file counts, images per condition, unmatched names, image sizes (px), pixel modes, all-zero captures, 8-bit grey-level summary, a starting `input` / `conditions` config block, warnings |
| 1 | `preview_enhance` | `config_path`, `presets`, `samples`, `overwrite` | step 1 if missing, `02_enhance-preview/` | contact sheet, metrics table, per-preset mean `edge_strength`, `flat_noise`, `p50` |
| 2 | `run_segment` | `config_path`, `mode` (`tune` \| `all`), `overwrite` | step 1 if missing, `04_segment-tune/` or `05_segment/` | `tune`: montages and mean objects per setting (all and >= `area_min_px`); `all`: label folder, objects per condition, Cellpose parameters, GPU use |
| 2 | `cut_distributions` | `config_path`, `area_min_px` (px^2 list), `frame_contact_px` (px list), `overwrite` | `<output_dir>/agent/cut-distributions_<hash>/` | cells kept per condition for every candidate pair, totals, histogram figure |
| 3 | `run_vampire` | `config_path`, `k` (list of int >= 2), `overwrite` | `09_vampire/` | elbow suggestions (second difference, kneedle), objects per display mode per K, figures, tables |
| any | `propose` | `config_path`, `stage`, `title`, `options` (id, label, description, figures, table, numbers, references, config_changes), `recommended`, `rationale`, `evidence`, `provenance`, `main_figure`, `figure_state` | `records/proposals/<id>.json` + figure copies | `proposal_id`, `sha256`, copied files |
| any | `list_pending` | `config_path` | nothing | `n_pending`, `pending` (id, stage, title, created, recommended, options) |
| any | `get_decision` | `config_path`, `proposal_id` | nothing | `decided`, `decision` (null until decided) |
| any | `build_report` | `config_path`, `out_dir`, `prefix` | `<prefix>_report_<YYMMDD>_c<N>.pptx` / `.md` | paths, slide count, snapshot names |

The agent never decides: decisions come only from the review page (or an imported download). File
formats, the guard and the report layout: [protocol.md](protocol.md).

### `cut_distributions`

* Objects come from `06_select/objects.csv` when step 6 has been run (image-trimmed objects are
  excluded); otherwise the step-5 label images are read and the largest-piece and image-trim rules
  of step 6 are applied in memory. `source_step` in the result says which.
* Each candidate pair is applied with the step-6 rule in its order: area minimum first, then the
  frame-contact maximum.
* Default candidates: `agent.cut.area_min_factors` x `select.area_min_px` and
  `agent.cut.frame_contact_factors` x `select.frame_contact_px`; the current values are always
  included and flagged `is_current`.
* Output folder `<tool>_<first 10 hex digits of SHA-256 of the parameters>`: `cut_table.csv`
  (one row per pair and condition: `n_objects_in`, `n_below_area_min`, `n_frame_contact`, `n_kept`,
  `n_images`, `n_images_without_kept`), `cut_kept_pivot.csv` (kept cells, pairs x conditions),
  `cut_histograms.png` / `.svg` / `source_cut_histograms.csv` (log10 area and frame-contact
  histograms with the candidate lines) and `run_manifest.json`.

### `run_segment` and `run_vampire`

These run the pipeline steps unchanged (see [pipeline.md](pipeline.md)). `run_segment` needs the
`segment` extra (Cellpose); `run_vampire` needs a VAMPIRE checkout and interpreter in the
`vampire` section of the configuration.

## Tests

`tests/test_mcp_tools.py` and `tests/test_records.py` call every tool function on synthetic images and synthetic label
images, checks the registered tool schemas and runs the server over stdio. Cellpose is replaced by
a threshold stand-in. The VAMPIRE test runs only when `MORPHOAGENT_TEST_VAMPIRE_SRC` (the
`Source Code` folder of a VAMPIRE_open checkout) and `MORPHOAGENT_TEST_VAMPIRE_PYTHON` (its
interpreter) are set. `tests/test_literature.py` starts both servers over stdio and lists their tools.
