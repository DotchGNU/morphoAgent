# Outputs

Back to the [README](../README.md#outputs).

All paths are relative to `output_dir`. Every step folder contains `run_manifest.json`:

```json
{"step": 6, "name": "06_select", "timestamp_utc": "...", "versions": {"python": "...", "numpy": "..."},
 "platform": "...", "config_file": "...", "config_sha256": "...",
 "parameters": {...}, "counts_in": {...}, "counts_out": {...}, ...step-specific extras}
```

## Figures

Every plot is written three times: `<name>.png` (300 dpi, transparent), `<name>.svg` (text kept
as text) and `source_<name>.csv` with exactly the plotted coordinates (e.g. jittered x and value of
every dot; pivoted fractions of stacked bars; contour points). Image montages (contact sheet,
tune montages, review overlays) are raster images with a `source_*.csv` describing the panels.

## Tables

| File | Rows | Key columns |
|---|---|---|
| `01_load/inventory.csv` | image | condition, field, file, path, height, width, max, mean, status (`ok` / `all_zero`) |
| `01_load/images_per_condition.csv` | condition | n_files, n_ok, n_skipped |
| `02_enhance-preview/preview_metrics.csv` | image × preset | p1, p50, p95, p99, edge_strength, flat_noise, is_default, info |
| `03_enhance/enhance_log.csv` | image | size, metrics, preset info |
| `04_segment-tune/tune_counts.csv` | image × setting | settings, is_default, n_objects, n_objects_area_ge_min, runtime_s |
| `05_segment/objects.csv` | object | image, condition, field, label, area_px … intensity_mean |
| `05_segment/images.csv` | image | n_objects, runtime_s |
| `06_select/objects.csv` | object | measurements, fragment_px_removed, group_rule, group |
| `06_select/cells.csv` | kept object | as above |
| `06_select/fields.csv` | image | label_tif, label_source (`segment` or `field_table`), label_input (label file read when a field table gave one), image_trimmed, n_objects, n_kept, n_frame_contact, n_below_area_min, n_fragmented_labels |
| `07_review/overlays/source_<image>_numbered.csv` | object | label, group, area, contact, shape |
| `08_curate/applied_edits.csv` | edit action | csv_row, image, operation, label, pixels, note (gap, filled pixels) |
| `08_curate/objects.csv` etc. | | as step 6 plus `touched` |
| `09_vampire/shape_modes_K<k>.csv` | object | shape_mode (display), raw_shape_mode, dist_from_center, metrics, vampire_area_px, area_abs_diff_px |
| `09_vampire/mode_mapping_K<k>.csv` | raw mode | n_objects, mean_aspect_ratio, display_mode |
| `09_vampire/mode_morphometry_K<k>.csv` | display mode | n_objects, mean shape metrics, area mean/median |
| `09_vampire/elbow_inertia.csv`, `elbow_suggestions.json` | K | inertia, n_iter, second_diff, kneedle_distance; suggested K |
| `09_vampire/vampire_dropped_objects_K<k>.csv` | object | cells VAMPIRE skipped (contour < 5 points) |
| `09_vampire/vampire_work/` | | image sets, job.json, runner.log, raw VAMPIRE outputs, normalised feature matrix |
| `10_stats/object_metrics.csv` | object | metrics and shape modes used for statistics |
| `10_stats/summary_by_condition.csv` | metric × condition | n, mean, sd, median, q1, q3, ratios, p_welch_vs_reference, q_bh, stars |
| `10_stats/mode_composition_K<k>.csv` | K × mode × condition | n_in_mode, n_condition, fraction, ratio, odds_ratio, p_fisher, q_bh, stars |
| `10_stats/shannon_K<k>.csv` | condition | shannon_H, ci_low, ci_high (only when enabled) |

## Label images

uint16 TIFF, 0 = background, same size as the raw image. `05_segment/labels` are Cellpose output;
`06_select/labels` have one piece per label; `08_curate/labels` contain only edited images.
Label numbers are stable across steps 5–8 (deletions leave gaps; merges keep the target number).

## Agent records and reports

`<output_dir>/agent/records/` (`agent.records_dir`) holds `proposals/`, `decisions/`, `snapshots/`
and a `run_manifest.json`; `morphoagent report --out DIR` writes
`<prefix>_report_<YYMMDD>_c<N>.pptx`, `.md`, a `_files/` folder of figures and `DIR/run_manifest.json`.
Formats: [protocol.md](protocol.md).
