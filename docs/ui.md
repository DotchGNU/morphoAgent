# Browser helper tools (`morphoagent ui`)

Back to the [README](../README.md#what-it-does).

Two commands help you practise a new analysis visually and record the choices in exactly the
format the pipeline reads:

| Command | What it gives you | Needs |
|---|---|---|
| `morphoagent ui build -c cfg.yaml [-o review.html] [--overwrite]` | ONE self-contained HTML file. Double-click it; it works offline from `file://` in current Chrome, Edge and Firefox | nothing; tab 1 uses step 2, tab 2 step 4, tabs 3-4 step 6 (step 3 for the background); a tab whose step has not been run shows a note. Tab 5 uses the proposal records |
| `morphoagent ui serve -c cfg.yaml [--host H] [--port P]` | The same page from a small local server, plus autosave to a file, live enhancement sliders and the optional assistant | as above |

Neither command changes any pipeline output. The page produces text you paste into your config
(YAML snippets), a `curation.csv` that `morphoagent curate` applies and, on tab 5, decisions on the
agent's proposals ([protocol.md](protocol.md)).

## The page

A sticky top bar holds the five tabs, a condition filter, a progress counter
(`done x/y | edits n in m field(s)`), **Import state** / **Export state** buttons and the save status.
Work is autosaved in the browser's local storage (keyed by a dataset id), and in serve mode also on
the server. `ui_state.json` (Export state) restores everything, including on another computer.

### Tab 1 — Enhancement (steps 2–3)

For each sample image of step 2 the page shows every preset preview with its metrics
(edge strength: higher = crisper; flat-background noise: lower = cleaner; p50 = median grey level).
For `figure-default` it also shows variants over `ui.median_target_grid` (default 105, 115, 125,
135, 145), computed by `ui build`. Green border = current config, blue = your choice. A wipe slider
compares the unenhanced image (left) with the selected preview (right); hovering over a card shows
that card in the wipe. The snippet to paste:

```yaml
enhance:
  preset: figure-default
  figure_default:
    median_target: 135.0
```

In serve mode each sample also has a live row: pick `figure-default` (`median_target`), `clahe`
(`clip_limit`) or `tone-unsharp` (`amount`), move the slider, **Render** (the server recomputes the
preset on that image, `POST /api/enhance`) and **Use these parameters**. Slider ranges are
`ui.median_target_range`, `ui.clahe_clip_range`, `ui.unsharp_amount_range`.

### Tab 2 — Segmentation settings (step 4)

The step-4 montages are cut back into one panel per setting (no new segmentation is run). Each
panel shows the setting id, object count and count above `select.area_min_px`. Green = the current
`segment:` setting (DEFAULT), blue = your choice; double-click enlarges. Snippet:

```yaml
segment:
  flow_threshold: 0.8
  cellprob_threshold: -2.0
  diameter: 60.0
  downscale: 0.125
```

### Tab 3 — Mask review (steps 7–8)

One field at a time: the enhanced image (step 3, or the raw image) with the step-6 label outlines,
drawn like the step-7 overlays: kept = solid red, frame contact = pale dashed, objects below
`area_min_px` not drawn (hover still identifies them). Label numbers are drawn at the deepest
inside point of each mask.

* **Delete tool** (`D`): click a mask to toggle DELETE (red fill).
* **Merge tool** (`M`): click two masks in sequence (the first turns yellow; `Esc` cancels). Merged
  masks are filled blue, their shared outline disappears and the number reads `label>target`.
  Clicking a mask of an existing merge extends it; merging two merges unites them. The target
  (surviving label) is the smallest label unless you choose another one in the side panel or press
  `T` over a mask. Shift+click merges in either tool.
* A label cannot be both deleted and merged (the page refuses and says why).
* Hover shows label id, area (full-resolution px²), frame contact (px) and group.
* `Z` / `Ctrl+Z` undo, `Y` / `Ctrl+Y` / `Ctrl+Shift+Z` redo (per field, unlimited within the session).
* `N` / `→` next field, `P` / `←` previous, `F` toggle "field done", wheel or `+` / `-` zoom,
  drag to pan, `0` fit, `O` outlines on/off, `L` numbers on/off. The keys are listed on screen.
* Field list filters: condition (top bar), "with edits", "not done".

Hit-testing uses the embedded label map itself (decoded from PNG into a `Uint16Array` through a
canvas), not polygons, so what you click is exactly the label in the label image. Only merge
touching masks: `morphoagent curate` refuses a merge whose pieces do not touch (it stops with
an error and writes nothing).

### Tab 4 — Export

* **Download curation.csv** — exactly the format of [`morphoagent curate`](pipeline.md): columns
  `operation,image,labels,target`; per image one `delete` row listing all deleted labels, then one
  `merge` row per merge with an explicit `target`.
* **Download ui_state.json** — the page state (schema `morphoagent-ui-state/1`) for Import state.
* A preview table of the edits and the CSV text, and the command to apply it:

```bash
morphoagent curate -c cfg.yaml --edits curation.csv --yes      # add --overwrite to replace an earlier 08_curate
morphoagent run -c cfg.yaml --from 9 --to 10                    # downstream steps then use the curated tables
```

`--edits` overrides `curate.edits_csv` (it works with `curate` and `run`).

### Tab 5 — Decisions

Proposals written by an agent (`propose`, see [protocol.md](protocol.md)) as cards: stage badge,
title, id and time, the agent's rationale, evidence and tool / parameters, and one box per option with
figure thumbnails (click to enlarge), the option's numbers and table rows, references (links to
PubMed / doi.org), the configuration changes it implies and a **recommended** badge. The tab title
shows the number of pending proposals; **pending only** hides decided cards. Displayed texts never
contain absolute paths (paths inside the configuration folder are shown relative to it, others as
`<path>/<file name>`); non-integer numbers in option tables are shown with 4 significant digits (the
proposal file keeps the full values). Results of Accept / Reject / Snapshot appear as a message in
the lower right corner.

To keep cards short, two things are collapsed and stay one click away:

* **Details** — the tool provenance (tool name and full parameter JSON) of a card, and the
  configuration changes of each option, are in collapsed **Details** sections.
* **Columns** — an option table shows its first `ui.card_table_max_columns` columns (default 6)
  and a **+N more column(s)** button that reveals the rest; at most `ui.card_table_max_rows` rows
  (default 8) are shown, and the number of further rows is stated. The full table is in the
  proposal file and in the report.

| Control | Serve mode | Offline page |
|---|---|---|
| **Accept this option** / **Reject all options** (+ comment box) | after a confirmation, `POST /api/decide` writes `decisions/<id>.json` and a snapshot; the card turns grey with the choice | downloads `<proposal_id>.json`; record it with `morphoagent decision -c cfg.yaml --import <file>` (writes the decision and its snapshot) |
| **Snapshot** (per option, or for the whole card) | `POST /api/snapshot` writes a new snapshot folder | disabled |
| **Refresh** | reloads proposals written after the page was opened | — |

Decisions are final; a card that changed on disk since it was loaded (hash mismatch) or that was
decided elsewhere is refused with a message and the list is reloaded. The page opens on this tab
when step 6 has not been run.

## File size of the offline page

Everything is embedded as base64 (about 4/3 of the binary size):

| Content | Encoding | Default size control |
|---|---|---|
| Field backgrounds | JPEG | `ui.max_width` (1280 px), `ui.jpeg_quality` (85) |
| Label maps | PNG, `label = R + 256·G`, nearest-neighbour to the same size as the background | `ui.max_width` |
| Enhancement previews | JPEG | `ui.preview_width_px` (960), number of `median_target_grid` values |
| Segmentation panels | JPEG at the montage panel size | `segment.tune.panel_width_px` |

Example: 12 fields of 2560 × 1920 px, 2 enhancement samples × 8 previews and 2 segmentation samples
× 24 settings gave a 13.5 MB file (backgrounds 4.6 MB, previews 4.1 MB, panels 4.4 MB, label maps
0.2 MB). Phase-contrast images compress poorly; lower `max_width` or `jpeg_quality` for smaller
files. Label maps at reduced width lose tiny objects: the page warns when a drawn label is absent
from the embedded map. Coordinates and areas shown are always full resolution.

## `morphoagent ui serve`

```bash
morphoagent ui serve -c cfg.yaml                      # http://127.0.0.1:8765/
```

| Endpoint | Purpose |
|---|---|
| `GET /` | the page (live mode) |
| `GET /api/fields` | field ids, conditions and object counts |
| `GET /api/state` | the autosaved state (empty object if none) |
| `POST /api/save` | autosave; body = state JSON (schema checked, unknown image ids refused); written atomically (temporary file + rename) to `ui.state_file`, default `<config stem>_ui_state.json` next to the config |
| `POST /api/enhance` | `{"image": id, "preset": name, "params": {...}}` → JPEG; metrics in the `X-BFM-Metrics` header; parameters are clamped to the slider ranges |
| `GET /api/suggest/<image>` | optional assistant (below) |
| `GET /api/proposals`, `POST /api/decide`, `POST /api/snapshot` | Decisions tab ([protocol.md](protocol.md#http-endpoints-morphoagent-ui-serve)) |

### Security model

* **No authentication = loopback only.** With the default `ui.host: 127.0.0.1` only programs on
  the same machine can reach the server.
* A non-loopback `ui.host` (or `--host`) **requires** a shared token: the environment variable
  named by `ui.token_env` (default `MORPHOAGENT_UI_TOKEN`) must hold at least 16 characters, otherwise
  the server refuses to start and says why. Every request must then carry the token
  (`X-BFM-Token` header; the first page load uses `?token=...`, which the page removes from the
  address bar and keeps for the tab session only). Tokens are compared in constant time and never
  logged; request logs omit query strings.
* The server speaks plain HTTP. Cross-origin requests are refused unless their origin is listed in
  `ui.cors_origins` (empty = CORS disabled). Request bodies are limited to `ui.max_request_bytes`.
* API keys are read from environment variables only and never written to any file.

### On a lab compute server

Preferred — keep the server on loopback and tunnel:

```bash
# on the compute server
morphoagent ui serve -c cfg.yaml                        # binds 127.0.0.1:8765
# on your computer
ssh -N -L 8765:127.0.0.1:8765 user@compute-server
# then open http://127.0.0.1:8765/ locally
```

Only if a tunnel is impossible, bind to the server's address with a token:

```bash
export MORPHOAGENT_UI_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
morphoagent ui serve -c cfg.yaml --host 10.0.0.5 --port 8765
# open http://10.0.0.5:8765/?token=<the token>  (anyone on the network path can read plain HTTP)
```

## Optional LLM assistant

> **Privacy notice.** When `ui.llm.enabled: true`, pressing **Ask assistant** sends the current
> field's numbered overlay image (downscaled to `ui.llm.image_width_px`) and a compact object table
> to `ui.llm.endpoint`. **Images and tables leave the machine.** Do not enable it for unpublished or
> sensitive data without permission from the data owner. The page shows the same notice while the
> assistant is enabled.

* Disabled by default; available in serve mode only. Calls happen only when the user presses the
  button for one field — there are no automatic or bulk calls — and at most
  `ui.llm.max_requests_per_minute` requests per minute are sent (HTTP 429 beyond that).
* Answers are validated (existing labels, known kinds, reasons cut to 300 characters) and cached in
  `ui.llm.cache_file` (default `<config stem>_llm_cache.json`); a cached answer is reused for an
  identical request.
* Suggestions appear as yellow badges and dots. **They never edit anything by themselves**: each
  one must be accepted (or dismissed) individually. Accepting `fragmented` with partner labels
  merges them; accepting `merged` or `not_a_cell` marks the label DELETE (the pipeline cannot split
  a mask). Accepted edits can be undone like any other edit.

```yaml
ui:
  llm:
    enabled: true
    provider: generic-json            # generic-json | chat-completions | mock | package.module:function
    endpoint: https://llm.example.org/v1/review
    model: my-vision-model
    api_key_env: MORPHOAGENT_LLM_API_KEY  # export MORPHOAGENT_LLM_API_KEY=... (sent as "Authorization: Bearer ...")
    timeout_s: 60
    max_requests_per_minute: 6
```

### Request (`generic-json` sends this document as the JSON body)

```json
{
  "task": "morphoagent.mask_review.v1",
  "model": "my-vision-model",
  "instructions": "You review automatic cell segmentation masks ... Answer with JSON only: {...}",
  "field": "CTRL_Image004",
  "image": {"mime_type": "image/jpeg", "width": 1024, "height": 768, "data_base64": "..."},
  "objects": [
    {"label": 12, "area_px": 15321.0, "frame_contact_px": 0, "group": "kept", "aspect_ratio": 1.8, "solidity": 0.91}
  ],
  "response_schema": {"suggestions": [{"label": "int", "kind": "merged|fragmented|not_a_cell", "with": "[int]", "reason": "str"}]}
}
```

`objects` lists drawn objects only (kept and frame contact), at most `ui.llm.max_objects` rows.
The instructions ask for labels that look **merged** (one mask covering several cells),
**fragmented** (one cell split into several masks; partner labels in `with`) or **not a cell**
(debris, background, artefact), each with a short reason.

### Response

```json
{"suggestions": [
  {"label": 12, "kind": "fragmented", "with": [13], "reason": "same cell body split by a bright ridge"},
  {"label": 31, "kind": "not_a_cell", "reason": "out-of-focus debris"}
]}
```

### Providers

| `provider` | Behaviour |
|---|---|
| `generic-json` | POST the request document; expect the response document |
| `chat-completions` | Send a `/v1/chat/completions`-style body (`model`, `messages` with a system text and a user message holding the table and the image as a `data:` URL); parse the JSON object in `choices[0].message.content`. Many self-hosted inference servers accept this shape |
| `mock` | Offline, deterministic (used by the tests) |
| `package.module:function` | Your own function `f(request: dict, settings: LLMConfig, api_key: str or None) -> dict` returning the response document; use it to adapt to any other API |

Implementation: `src/morphoagent/pipeline/ui/llm.py` (`build_request`, providers, `validate_response`,
`RateLimiter`, `SuggestionCache`).

## Known limitations

* The offline page cannot call the assistant or recompute enhancements (no server); use
  `ui serve` for those.
* Downloads use the browser's download mechanism; the page cannot write next to your data by itself
  in offline mode (serve mode autosaves to `ui.state_file`).
* Merge validity (touching masks) is checked by `morphoagent curate`, not by the page.
* The page holds all images in memory; very large datasets are better split (one config per batch).
