---
name: segment-cut
description: Stage 2 segmentation and cut-offs. Use after the contrast proposal is decided - tune the Cellpose-SAM setting on sample images, segment all images, propose area-minimum and frame-contact cut-offs from cut_distributions, then build the numbered review and offer optional curation. Two decisions - segmentation setting, then cut-offs.
---

# Stage 2 - segmentation and cut-offs

## When to use

The `contrast` proposal has a decision. This stage has two cards in order: `segment` (Cellpose
setting), then `cut` (area minimum and frame-contact maximum). Curation is optional and decided by
the researcher on the review page.

## 2a. Segmentation setting

1. `list_pending(config_path)` - stop if a `consult` or `contrast` proposal is pending.
2. `run_segment(config_path, mode="tune")` - grid of `segment.tune` values on sample images. Read
   `per_setting` (mean objects per image, all and >= `area_min_px`), `montages`, `tune_counts_csv`.
   The current setting is in the grid and marked.
3. Look at the montages. Judge outlines (one outline per cell, no merged neighbours, no background
   fragments) and quote counts from `tune_counts.csv`.
4. `propose(stage="segment", title="Segmentation setting", ...)`: one option per candidate setting
   (usually 2-4, not the whole grid); `figures` = the montage PNGs; `numbers` = mean objects per
   image (all, >= area minimum); `config_changes` = e.g. `{"segment.cellprob_threshold": -2,
   "segment.diameter": 60, "segment.downscale": 0.125}`.
5. Wait for `get_decision`; write the chosen values to the config.
6. `run_segment(config_path, mode="all")` - every image. Report `n_images`, `n_objects`,
   `objects_per_condition`, `gpu_used`.

## 2b. Cut-offs

1. `cut_distributions(config_path)` - default candidates are factors of the current
   `select.area_min_px` and `select.frame_contact_px` (`agent.cut.*`); pass explicit
   `area_min_px` / `frame_contact_px` lists if the histograms suggest other values.
2. Read `totals` (kept of `n_objects_in` per pair), `rows` (per condition; `n_images_without_kept`),
   `current`, and the histogram figure (`cut_histograms.png`, log10 area and frame contact with the
   candidate lines).
3. Check balance: a cut that removes a much larger share in one condition than in another changes
   the comparison. Quote kept / total per condition for each candidate.
4. `propose(stage="cut", title="Cell cut-offs", ...)`: options = 2-4 candidate pairs (include the
   current one); `figures` = `[cut_histograms.png]`; `table` = that pair's rows per condition;
   `numbers` = total kept, total in; `config_changes` = `{"select.area_min_px": ...,
   "select.frame_contact_px": ...}`.
5. Wait for `get_decision`; write the values to the config.

## 2c. Numbered review and optional curation

1. Run steps 6-7 (shell): `morphoagent run -c <config> --from 6 --to 7`. Report the kept counts from
   `06_select/run_manifest.json` (count tree) and the review deck path `07_review/review_deck.pptx`.
2. Tell the researcher how to review: `morphoagent ui serve -c <config>` (tabs 3-4: tick masks to
   delete / merge, export `curation.csv`), or the review deck (numbers on each object).
3. Ask whether curation is needed. Only if the researcher provides a curation file:
   `morphoagent curate -c <config> --edits <curation.csv> --yes`. Report `applied_edits.csv` counts.
   Otherwise `morphoagent curate -c <config> --skip`.

## Stop conditions

* Stop after each `propose`; never run `mode="all"` before the segment decision, and never run
  step 6 before the cut decision.
* Stop if `run_segment` reports Cellpose missing (the `segment` extra) or no GPU when the researcher
  expected one; report it.
* Stop and tell the researcher if the chosen cut leaves a condition with no kept objects
  (`n_kept` 0 in `rows`) or images with a single kept object (VAMPIRE cannot read such a set; see the
  README troubleshooting table).
* Never edit label images or input images by hand; curation goes through the CSV only.
