# Configuration keys

Back to the [README](../README.md#quick-start-b-without-an-agent).

The complete default file is [`configs/default.yaml`](../configs/default.yaml); it is identical to
the dataclasses in `src/morphoagent/pipeline/config.py` (a test enforces this). A user config contains only the
keys it changes. Unknown keys are errors. Relative paths are resolved relative to the config file.
`morphoagent init-config FILE` writes the defaults.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `output_dir` | `/path/to/output` | Root of all step folders |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` (CLI `--log-level` overrides) |

## `input`

| Key | Default | Meaning |
|---|---|---|
| `image_dir` | `/path/to/images` | Folder with the images (not searched recursively) |
| `extensions` | `[.jpg, .jpeg, .png, .tif, .tiff]` | Case-insensitive |
| `strip_prefix` | `''` | Removed from the start of the file stem before parsing |
| `strip_suffix` | `''` | Removed from the end of the stem |
| `name_regex` | `^(?P<condition>.+)_(?P<field>Image\d+)$` | Must define groups `condition` and `field` |
| `skip_max_at_or_below` | `0` | Images whose maximum pixel is ≤ this are skipped |

## `conditions`

| Key | Default | Meaning |
|---|---|---|
| `order` | `[]` | Order in tables and plots; others follow alphabetically |
| `reference` | `null` | Reference for ratios and tests; null = first in order |
| `colors` | `{}` | Condition → fill colour |
| `default_color` | `'#FFFFFF'` | Fill for conditions without a colour |

## `enhance` (steps 2, 3)

| Key | Default | Meaning |
|---|---|---|
| `preset` | `figure-default` | Preset applied in step 3 |
| `samples` | `null` | Image ids for step 2; null = first image of the first N conditions |
| `max_sample_conditions` | `4` | N above |
| `preview_presets` | all four | Presets shown in step 2 |
| `metrics_width_px` | `448` | Width (px) images are resized to before metrics |
| `sheet_panel_width_px` | `480` | Panel width in the contact sheet |
| `edge_top_fraction` | `0.02` | Fraction of strongest Sobel responses in `edge_strength` |
| `noise_sigma_px` | `1.5` | Gaussian σ subtracted for `flat_noise` |
| `flat_gradient_sigma_px` | `3.0` | Gaussian σ of the gradient that defines flat pixels |
| `flat_fraction` | `0.3` | Fraction of flattest pixels used for `flat_noise` |
| `figure_default.output_size` | `[960, 720]` | [width, height] after Lanczos reduction; null keeps the size |
| `figure_default.median_target` | `125.0` | Grey level of the image median after the shift |
| `figure_default.kernel_path` / `tone_lut_path` | `null` | Replacement `.npy` files; null = packaged fit |
| `tone_unsharp.source_percentiles` | `[1, 50, 95, 99]` | Percentiles of the image … |
| `tone_unsharp.target_values` | `[62, 179, 235, 255]` | … mapped to these grey levels |
| `tone_unsharp.sigma_px` | `4.0` | Unsharp-mask Gaussian σ (input px) |
| `tone_unsharp.amount` | `null` | Fixed amount; null = choose on the grid |
| `tone_unsharp.amount_grid` | `[0.0, 8.0, 0.1]` | start, stop, step |
| `tone_unsharp.display_height_px` | `503` | Height at which the edge metric is measured |
| `tone_unsharp.reference_edge_metric` | `3.5468` | Edge metric of a reference figure panel |
| `tone_unsharp.target_fraction` | `1.2` | Target = base + fraction × (reference − base) |
| `clahe.clip_limit` / `kernel_size` / `nbins` | `0.01` / `null` / `256` | skimage `equalize_adapthist` |

## `segment` (steps 4, 5)

| Key | Default | Meaning |
|---|---|---|
| `model` | `cpsam` | Cellpose pretrained model |
| `gpu` | `true` | Use CUDA if available |
| `flow_threshold` | `0.8` | Cellpose flow error threshold |
| `cellprob_threshold` | `-1.0` | Cellpose cell probability threshold |
| `diameter` | `null` | Cellpose diameter in px of the downscaled image; null = Cellpose default |
| `downscale` | `0.125` | Resize factor before Cellpose |
| `label_compression` | `zlib` | TIFF compression of label images |
| `tune.samples`, `tune.max_sample_conditions` | `null`, `4` | Sample images for step 4 |
| `tune.flow_threshold` | `[0.8]` | Grid values |
| `tune.cellprob_threshold` | `[-1, -2, -3, -4]` | Grid values |
| `tune.diameter` | `[null, 60, 100]` | Grid values |
| `tune.downscale` | `[0.125, 0.25]` | Grid values |
| `tune.panel_width_px` | `480` | Montage panel width |
| `tune.outline_color` | `[255, 40, 40]` | RGB |

## `measure` (steps 5, 6 and the `cut_distributions` tool)

| Key | Default | Meaning |
|---|---|---|
| `shape_grid` | `full` | `full`: every feature on the full-resolution labels. `segmentation`: `major_axis_length`, `minor_axis_length`, `aspect_ratio` and `eccentricity` are measured on the labels sampled on the segmentation grid (`segment.downscale`), axis lengths converted back to full-resolution px. Objects changed after segmentation (largest piece, curation, off-grid edits, objects differing from a field table's `segmentation_label_file`) stay full resolution. A field table column `shape_grid` overrides it per field. See [pipeline.md](pipeline.md#where-shape-features-are-measured) |

## `select` (step 6)

| Key | Default | Meaning |
|---|---|---|
| `largest_piece` | `true` | Keep only the largest connected piece per label |
| `connectivity` | `8` | 4 or 8 |
| `area_min_px` | `2000.0` | Minimum area (full-resolution px²) |
| `frame_contact_px` | `24.0` | Maximum number of pixels on the image frame |
| `image_trim_fraction` | `0.0` | Fraction of most atypical images dropped per condition |
| `image_trim_min_images` | `2` | Minimum images kept per condition |
| `image_trim_features` | five shape metrics | Per-image medians used for the trim |
| `image_trim_log_area` | `true` | Add median log10 area |
| `field_table` | `null` | CSV with columns `image` (required), `label_file`, `condition`, `shape_grid`, `segmentation_label_file` (optional): step 6 uses only the listed fields, and a given `label_file` instead of the step-5 labels. Relative paths resolve against the CSV's folder. See [pipeline.md](pipeline.md#field-table) |

## `review` (step 7)

| Key | Default | Meaning |
|---|---|---|
| `background` | `enhanced` | `enhanced` (step 3) or `raw` |
| `kept_color`, `kept_linewidth_px` | `[255,40,40]`, `6` | Kept outlines |
| `frame_contact_color`, `frame_contact_linewidth_px`, `dash_px` | `[255,150,150]`, `3`, `22` | Frame-contact outlines |
| `font_path` | `null` | TrueType font; null = DejaVu Sans Bold |
| `font_scale`, `font_min_px`, `font_max_px` | `0.22`, `38`, `78` | Font size = clip(scale·√area, min, max) |
| `text_color`, `stroke_color` | white, black | Number colours |
| `stroke_min_px`, `stroke_divisor` | `3`, `12` | Stroke = max(min, size // divisor) |
| `slide_image_width_px`, `jpeg_quality` | `1600`, `90` | Image compression inside the deck |

## `curate` (step 8)

| Key | Default | Meaning |
|---|---|---|
| `edits_csv` | `null` | Curation CSV (`operation,image,labels,target`) |
| `connectivity` | `8` | Connectivity required for a merged object |

## `vampire` (step 9)

| Key | Default | Meaning |
|---|---|---|
| `src_path` | placeholder | `Source Code` folder of a VAMPIRE_open checkout |
| `python` | `python` | Interpreter of the VAMPIRE environment |
| `n_coordinates` | `50` | Contour points per object |
| `k` | `[4]` | Number(s) of shape modes |
| `renumber_by` | `aspect_ratio` | Display modes by ascending mean aspect ratio, or `none` |
| `elbow.k_min`, `elbow.k_max` | `2`, `20` | Elbow range |
| `elbow_kmeans` | k-means++, 3, 300, 9 | Must match VAMPIRE's internal k-means for comparable inertia |
| `mode_colors`, `contour_edge_color` | 10 colours, `#3A4650` | Mode figure colours |

## `stats` (step 10)

| Key | Default | Meaning |
|---|---|---|
| `metrics` | area, aspect ratio, form factor, solidity, eccentricity, extent, perimeter | Columns of the cells table |
| `pixel_size_um` | `null` | Pixel size in um/px; adds `area_um2` |
| `pixel_calibration.field_width_um`, `image_width_px`, `binning` | `null`, `null`, `1.0` | Alternative to `pixel_size_um` (set one, not both): pixel size = `field_width_um / image_width_px / binning`, computed in that order in double precision. Use it when the calibration is known as a field width, so the value is derived rather than a rounded number. The value used is written to the step-10 manifest as `pixel_size_um_effective` |
| `significance_from` | `p` | Stars from uncorrected p or BH q |
| `star_thresholds` | `[0.05, 0.01, 0.001]` | Decreasing |
| `box.*` | see default.yaml | Box-plot geometry, line widths (pt), jitter seed, `y_top` (`whisker` / `data`) |
| `shannon.enabled`, `n_boot`, `seed`, `ci` | `false`, `1000`, `20261105`, `95` | Optional diversity |

## `ui` (`morphoagent ui build` / `morphoagent ui serve`, see [ui.md](ui.md))

| Key | Default | Meaning |
|---|---|---|
| `max_width` | `1280` | Field images and label maps are embedded at most this wide (px) |
| `jpeg_quality` | `85` | JPEG quality of embedded and live images |
| `preview_width_px` | `960` | Width of the enhancement previews |
| `median_target_grid` | `[105, 115, 125, 135, 145]` | `figure-default` variants precomputed by `ui build` |
| `html_path` | `null` | Output of `ui build`; null = `<output_dir>/ui/review.html` |
| `host`, `port` | `127.0.0.1`, `8765` | Bind address of `ui serve`; a non-loopback host requires a token |
| `token_env` | `MORPHOAGENT_UI_TOKEN` | Environment variable holding the shared access token (>= 16 characters) |
| `cors_origins` | `[]` | Origins allowed to call the API from other pages; empty = CORS disabled |
| `state_file` | `null` | Autosave file; null = `<config stem>_ui_state.json` next to the config |
| `max_request_bytes` | `20000000` | Largest accepted request body |
| `median_target_range`, `clahe_clip_range`, `unsharp_amount_range` | `[60, 200, 1]`, `[0.001, 0.05, 0.001]`, `[0, 8, 0.1]` | Live slider [min, max, step]; server clamps to these |
| `card_table_max_rows` | `8` | Decisions tab: rows of an option table shown on the card (the remaining number is stated; the full table is in the proposal file and the report) |
| `card_table_max_columns` | `6` | Decisions tab: columns shown before a "+N more columns" expander |
| `llm.enabled` | `false` | Optional assistant; images and tables leave the machine when enabled |
| `llm.provider` | `generic-json` | `generic-json`, `chat-completions`, `mock` or `package.module:function` |
| `llm.endpoint`, `llm.model` | `null` | URL and model name of the LLM endpoint |
| `llm.api_key_env` | `MORPHOAGENT_LLM_API_KEY` | Environment variable holding the API key (sent as a Bearer token) |
| `llm.timeout_s` | `60.0` | Request timeout (s) |
| `llm.max_requests_per_minute` | `6` | Rate limit; there are no automatic bulk calls |
| `llm.image_width_px`, `llm.jpeg_quality` | `1024`, `85` | Downscaled overlay sent per request |
| `llm.max_objects` | `400` | Object-table rows sent per field |
| `llm.cache_file` | `null` | Response cache; null = `<config stem>_llm_cache.json` next to the config |

## `plot`

| Key | Default | Meaning |
|---|---|---|
| `dpi` | `300` | PNG resolution |
| `font_family` | `Arial` | Falls back to the matplotlib default with a warning |
| `font_paths` | `[]` | Extra `.ttf` files to register |
| `font_size_pt` | `8.0` | Base font size |

## `agent` (`morphoagent-mcp` and the decision records, see [mcp.md](mcp.md), [protocol.md](protocol.md))

| Key | Default | Meaning |
|---|---|---|
| `output_subdir` | `agent` | Tool outputs that are not pipeline steps go to `<output_dir>/<output_subdir>/<tool>_<parameter hash>/` |
| `records_dir` | `null` | Proposals, decisions, snapshots ([protocol.md](protocol.md)); `null` = `<output_dir>/<output_subdir>/records`; relative paths resolve against the config file |
| `require_decisions` | `true` | Stage tools refuse to run while a proposal of an earlier stage is undecided |
| `thumbnail_width_px` | `360` | Width of the figure thumbnails on decision cards (px, >= 64) |
| `report.prefix` | `morphoagent` | `morphoagent report` file name `<prefix>_report_<YYMMDD>_c<N>.pptx` / `.md` |
| `report.max_table_rows` | `12` | Option rows in the slide table (the chosen row is always shown; the rest are counted in a note) |
| `report.max_number_columns` | `12` | Option `numbers` keys written as table columns to the Markdown report |
| `report.max_table_columns` | `6` | At most this many numeric columns on a slide table (fewer when the minimum widths do not fit at 8 pt); the rest appear in the Markdown report only |
| `report.font_family` | `Arial` | Slide text font |
| `report.redact_paths` | `true` | On slides and in the Markdown report, absolute paths (in provenance parameters, rationale, comments, table cells) are shown relative to the configuration folder when inside it, otherwise as `<path>/<file name>`. Proposals, decisions, `note.json` and run manifests keep the full paths |
| `cut.area_min_factors` | `[0.25, 0.5, 1.0, 1.5, 2.0]` | `cut_distributions`: area cut-off candidates = factor x `select.area_min_px` (px^2) |
| `cut.frame_contact_factors` | `[0.0, 0.5, 1.0, 2.0, 4.0]` | `cut_distributions`: frame-contact candidates = factor x `select.frame_contact_px` (px) |
| `cut.histogram_bins` | `60` | Bins of the log10(area) and frame-contact histograms |

## `literature` (`morphoagent-literature`, see [literature.md](literature.md))

| Key | Default | Meaning |
|---|---|---|
| `tool_name` | `morphoagent` | Sent to NCBI as `tool` |
| `ncbi_email` | `''` | Sent to NCBI as `email` only when set |
| `ncbi_email_env` | `MORPHOAGENT_NCBI_EMAIL` | Environment variable read when `ncbi_email` is empty |
| `ncbi_api_key_env` | `MORPHOAGENT_NCBI_API_KEY` | Environment variable holding an optional NCBI API key |
| `eutils_url`, `europepmc_url` | NCBI E-utilities, Europe PMC REST | Base URLs (https only) |
| `ncbi_requests_per_second` | `null` | null = 3 without an API key, 10 with one; never above those |
| `europepmc_requests_per_second` | `5.0` | Europe PMC request rate (requests/s) |
| `timeout_s` | `20.0` | Per-request timeout (s) |
| `max_retries`, `backoff_s` | `3`, `1.0` | Retries on HTTP 429 / 5xx and network errors; wait `backoff_s` x 2^attempt (s) or `Retry-After` |
| `cache_dir` | `null` | Response cache; null = the user cache folder (relative paths resolve against the config file) |
| `cache_ttl_days` | `30.0` | Older cache entries are fetched again (0 = never reuse) |
| `offline` | `false` | Answer from the cache only |
| `max_results` | `50` | Upper bound of `search_literature` `max_results` (1-200) |
| `title_match_threshold` | `0.9` | `verify_citation` minimum title similarity (0-1] |
| `methods_max_paragraphs` | `200` | `fetch_methods_text` paragraphs returned at most |
