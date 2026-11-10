# Pipeline reference

Back to the [README](../README.md#the-ten-pipeline-steps).

Each step: purpose, inputs, outputs, parameters (config keys) with defaults and rationale.
Pixel units are pixels of the **original** image unless stated otherwise. Image ids are
`<condition>_<field>` everywhere.

---

## 1 · load

**Purpose.** Build the image inventory that every later step reads.

**Inputs.** `input.image_dir` (flat folder, files with `input.extensions`).

**Logic.** For each file stem: remove `strip_prefix` / `strip_suffix`, match `name_regex`
(named groups `condition`, `field`; conditions may contain underscores because the default
`condition` group is greedy and the field is anchored at the end). Unmatched names and duplicate
image ids are errors. Each image is read as 8-bit grey (identical RGB channels are reduced without
arithmetic; >8-bit integers are scaled from the dtype range). Images with maximum ≤
`skip_max_at_or_below` (default 0 = all-zero captures) get `status = all_zero` and are skipped by
every later step.

**Outputs.** `inventory.csv`, `images_per_condition.csv`, `skipped_all_zero.csv`.

---

## 2 · enhance-preview

**Purpose.** Show what each enhancement preset does so that one can be chosen.

**Inputs.** Step 1. Sample images: `enhance.samples` or, by default, the first image of each of the
first `max_sample_conditions` (4) conditions.

**Logic.** Every preset in `preview_presets` is applied to every sample. Metrics are computed on the
enhanced image after Lanczos resize to `metrics_width_px` (448) so that presets with different
output sizes are compared at the same scale:

* `p1, p50, p95, p99` — grey-level percentiles;
* `edge_strength` — mean of the strongest 2 % Sobel magnitudes divided by (p99 − p1);
* `flat_noise` — standard deviation of (image − Gaussian σ 1.5 px) over the 30 % of pixels with the
  lowest gradient magnitude of Gaussian σ 3 px (the flattest background).

**Outputs.** `contact_sheet.png` (+ `source_contact_sheet.csv` describing the panels),
`preview_metrics.csv`, full-size previews in `images/`.

### Presets

| Preset | What it does | Parameters |
|---|---|---|
| `figure-default` (default) | An empirical sharpen + tone preset fitted to reproduce a reference picture-correction (sharpness 100 %, brightness +20 %, contrast +50 %); fit residual about 5 grey levels; not an exact re-implementation of any software. Lanczos reduce to 960×720 → shift the median to 125 → `u = Σ_r w_r · ringsum_r(img)` with 13 ring weights (radius 12 px of the reduced image; binary ring masks, ring r covers distances [r−0.5, r+0.5), ring 0 = centre pixel; weights per pixel sum to 1; reflect padding) → piecewise-linear tone curve, clip 0–255, round. The two fitted arrays ship as `src/morphoagent/pipeline/data/figure_kernel.npy` and `figure_tone_lut.npy` (`[knots, values]`). | `output_size` [960, 720] or null (kernel radius is then in original px), `median_target` 125, optional `kernel_path`, `tone_lut_path` |
| `tone-unsharp` | Percentile tone map (the image's own p1/p50/p95/p99 → 62/179/235/255; 0 → 0, 255 → 255) + unsharp mask (Gaussian σ 4 px). The amount is chosen on the grid 0…8 (step 0.1) so that the edge metric at display scale (height 503 px) is closest to `base + 1.2 × (3.5468 − base)`, or fixed via `amount`. | `source_percentiles`, `target_values`, `sigma_px`, `amount`, `amount_grid`, `display_height_px`, `reference_edge_metric`, `target_fraction` |
| `clahe` | scikit-image `equalize_adapthist` | `clip_limit` 0.01, `kernel_size` null, `nbins` 256 |
| `none` | unchanged grey image | — |

---

## 3 · enhance

**Purpose.** Apply `enhance.preset` to every valid image for display and review.

**Outputs.** `images/<condition>_<field>_enhanced.png`, `enhance_log.csv` (size, metrics, preset
information per image).

**Rationale.** Enhancement is never used for segmentation or measurement (step 5 reads the raw
image), so changing the preset never changes any number downstream.

---

## 4 · segment-tune

**Purpose.** Show Cellpose-SAM outlines for a grid of settings on sample images.

**Parameters.** `segment.tune`: `cellprob_threshold` [−1, −2, −3, −4], `diameter` [null, 60, 100]
(px of the downscaled image; null = Cellpose default), `downscale` [0.125, 0.25],
`flow_threshold` [0.8]; sample selection as in step 2. The step-5 setting is always included and
flagged `DEFAULT`.

**Outputs.** `settings.csv`, `tune_counts.csv` (objects and objects ≥ `select.area_min_px` per
setting and image, runtime), `montages/<image>_tune-montage.png` with `source_*.csv` (panel
layout). Panels show the step-3 image when available, otherwise the raw image.

---

## 5 · segment

**Purpose.** Segment all images and measure every object at full resolution.

**Logic.** raw grey image → scikit-image `resize` by `downscale` (bilinear, anti-aliased, cast to
uint8) → Cellpose `eval` with `flow_threshold`, `cellprob_threshold` and, only if set, `diameter`
→ labels upscaled to the original size by nearest neighbour (uint16 TIFF) → `regionprops` on the
full-resolution label image.

**Defaults and rationale.** `model: cpsam`, `flow_threshold: 0.8` (above the Cellpose default 0.4,
which discards many masks at ambiguous junctions of touching cells), `cellprob_threshold: -1.0`,
`diameter: null`, `downscale: 0.125` (large cells at 20× are segmented well at 1/8 resolution and
the run is fast). These are the settings of the production runs the package was consolidated from.

**Measurements** (`objects.csv`): `area_px`, `perimeter_px`, `eccentricity`, `solidity`, `extent`,
`major_axis_length`, `minor_axis_length`, `aspect_ratio` = major/minor, `form_factor` =
4π·area/perimeter², `border_contact_px` = number of label pixels on the outermost image rows and
columns, `equiv_diameter_px`, centroid, `intensity_mean` (raw grey), `shape_grid` (where the axis
features were measured, see below).

### Where shape features are measured

`measure.shape_grid` (default `full`) decides where the axis features are measured:

| value | `major_axis_length`, `minor_axis_length`, `aspect_ratio`, `eccentricity` | all other features |
|---|---|---|
| `full` | full-resolution labels | full-resolution labels |
| `segmentation` | labels sampled on the segmentation grid (size `round(H × downscale) × round(W × downscale)`; grid pixel *i* takes full-resolution pixel `floor((i + 0.5) × H / h)`), axis lengths divided by `downscale` | full-resolution labels |

For labels made by step 5 the sampled grid is exactly the Cellpose output, so `segmentation`
gives the values `regionprops` returns on the Cellpose result (some earlier analyses measured
there). The two grids differ most for thin objects: on a coarse grid the second moments of an
elongated object differ from those of its nearest-neighbour upscaled mask.

An object keeps full-resolution axis values in `segmentation` mode when it is not a faithful
upscaling of its grid mask (drawn or edited at full resolution), when it vanishes on the grid, when
step 6 removed pieces of it (largest piece), when step 8 re-measures it after an edit, or when it
differs from the field table's `segmentation_label_file` (below). `shape_grid` in `objects.csv`
records which applied per object; the step-6 manifest counts objects and kept cells per value.

---

## 6 · select

**Order (fixed).**

1. **Largest piece** (`largest_piece: true`, `connectivity: 8`): pixels of a label outside its
   largest connected piece are removed; affected images are re-measured.
2. **Image trim** (`image_trim_fraction`, default 0.0 = off): per image, the vector of medians of
   `image_trim_features` (form factor, solidity, eccentricity, aspect ratio, extent) plus the median
   log10 area is z-scored over all images (population SD); within each condition the images
   farthest from the condition centroid are dropped, keeping `floor(n × (1 − fraction))` but at least
   `image_trim_min_images` (2). Applied before the object filters, on all objects of the image.
   A fraction of 0.2 has been used in practice.
3. **Area** (`area_min_px: 2000`): smaller objects → `below_area_min` (debris, fragments).
4. **Frame contact** (`frame_contact_px: 24`): objects with more than 24 px on the image frame →
   `frame_contact`. A yes/no "touches the border" rule drops large cells whose single process
   reaches the edge; a contact length keeps those and still drops cells cut by the frame.

### Field table

`select.field_table` names a CSV that fixes **which fields** are analysed and, per field, **which
label image** is used:

```csv
image,label_file,condition,note
CTRL_Image004,curated/CTRL_Image004_labels_edited.tif,CTRL,two merged cells split by hand
CTRL_Image006,,CTRL,
DRUG_A_Image010,other_tool/DRUG_A_Image010.tif,DRUG_A,
```

* `image` (required): image id of step 1. Fields not listed are not analysed; listing an image
  that step 1 skipped or does not know is an error, as is listing an image twice.
* `label_file` (optional): a label TIFF of the full image size with integer labels. Empty = the
  step-5 labels of that field (step 5 is then required). Relative paths resolve against the
  folder of the CSV. Objects of a given file are measured at full resolution in step 6, exactly as
  step-5 labels are.
* `condition` (optional): must equal the condition parsed in step 1 (guards against a table
  written for another image set).
* `shape_grid` (optional): `full` or `segmentation` overrides `measure.shape_grid` for this field
  (for example a field whose objects were all re-measured at full resolution in an earlier
  analysis). Empty = the configured value.
* `segmentation_label_file` (optional): the segmentation output that `label_file` was derived
  from. In `segmentation` mode, objects whose mask differs from the same label in this file
  (pixels gained or lost) are measured at full resolution; unchanged objects on the grid.
  Other columns are ignored.

The usual selection rules (largest piece, image trim, area minimum, frame contact) are applied to
every listed field. `fields.csv` records `label_source` (`segment` / `field_table`) and
`label_input`; the manifest records the table's path, SHA-256 and counts. Use it to reproduce an
analysis whose field set and hand-corrected label files were chosen by a person, or to analyse
label images from another segmentation tool. Label files are read only; step 6 writes its own copy.

**Outputs.** `labels/`, `objects.csv` (all objects; `group` = kept / frame_contact /
below_area_min / image_trimmed; `group_rule` = group without the image trim), `cells.csv` (kept),
`fields.csv` (per-image counts, `label_tif` relative to `output_dir`). The manifest holds a
`selection_tree` with counts after every rule and per-condition counts.

---

## 7 · review

**Purpose.** Let a person check every mask and mark labels to delete or merge.

**Logic.** For each field, the step-3 image (resized to the label size; or the raw image with
`background: raw`) is overlaid with kept objects (red solid, 6 px), frame-contact objects (pale red,
dashed, 3 px); objects below `area_min_px` are not drawn. The label number is printed at the
deepest interior point (maximum of the distance transform), white with a black stroke, font size
`clip(0.22·√area, 38, 78)` px. Image-trimmed fields are drawn by their rule groups and marked in
the slide title.

**Outputs.** `overlays/<image>_numbered.png` (+ `source_<image>_numbered.csv`), `review_deck.pptx`
(16:9, one slide per field: image | overlay), `curation_template.csv`.

---

## 8 · curate (optional)

**Behaviour.** Skips with exit code 0 when `--skip` is given or `curate.edits_csv` is unset or
missing. Otherwise asks `Is curation needed? [y/N]` unless `--yes` is given (a non-interactive
session without a flag skips).

**Edits.** `delete` sets the labels to background. `merge` relabels all listed labels to `target`
(default: the smallest), fills unlabelled pixels enclosed by the merged object
(`binary_fill_holes` restricted to background) and requires the result to be one connected
component (`curate.connectivity`). Edits run in CSV order.

**Measurement.** The edited image is re-measured at full resolution; objects whose pixels did not
change keep their stored values (`touched = False`). Groups are recomputed with the step-6 area and
frame-contact rules; image-trim decisions are kept.

**Outputs.** `labels/<image>_labels_curated.tif` (edited images only; step-6 files are untouched),
`objects.csv`, `cells.csv`, `fields.csv`, `applied_edits.csv`. Steps 9 and 10 use these tables when
the manifest says `status: applied`.

---

## 9 · vampire

**Purpose.** Unsupervised shape modes (Phillip et al., *Nat Protoc* 2021).

**Logic.** One image set per condition is written (label TIFFs containing only kept cells); all
sets are pooled into one model per K. VAMPIRE registers each contour with `n_coordinates` (50)
points, runs PCA (VAMPIRE uses the first 20 PCs), Box-Cox + z-score + L2 normalisation and k-means
(k-means++, n_init 3, random_state 9, fixed in VAMPIRE). The model is then applied to every set.

**Why internals are wrapped.** VAMPIRE does not return the normalised feature matrix, the PCA
eigenvalues, or the registered contours. `vampire_runner.py` wraps `KMeans.fit`, `PCA_custom` and
`clusterSM` in memory to record them (the originals run unchanged; no VAMPIRE file is modified). The
recorded matrix is used for the elbow, so the elbow inertia at the chosen K equals VAMPIRE's own fit
(checked and written to `elbow_suggestions.json`).

**Post-processing.** Elbow: inertia for K = `elbow.k_min`…`k_max` on the normalised matrix with
`elbow_kmeans`; suggestions = maximum second difference and kneedle (maximum distance below the
chord of min-max normalised curve). Modes are renumbered 1…K by ascending mean aspect ratio
(`renumber_by`); `mode_mapping_K*.csv` gives raw ↔ display. VAMPIRE's object order is mapped back
to label ids, and VAMPIRE's own area is compared with `area_px` (`area_abs_diff_px`, expected 0).

**Outputs.** see [outputs.md](outputs.md).

---

## 10 · stats

**Object-level only.** Every object is one observation; no per-image statistics are computed.

* `summary_by_condition.csv`: per metric and condition — n, mean, SD, median, Q1, Q3, ratio of mean
  and of median to the reference, Welch t-test p versus the reference (uncorrected) and the
  Benjamini–Hochberg adjusted q over conditions within the metric.
* `mode_composition_K*.csv`: counts and fractions per mode; Fisher exact test of (mode vs rest) ×
  (condition vs reference), odds ratio, p, BH q over conditions within each mode.
* Figures: box + jitter per metric (box Q1–Q3, bold median line, bold × at the mean, a black dot
  for every object, per-condition fill from `conditions.colors`, Arial, no legend, n in tick labels,
  0.25 pt lines except median/mean 1.4 pt, y-axis top = first tick above the highest upper whisker,
  dots above it are counted in the source table); stacked mode-fraction bars; per-mode fraction bars
  with stars (from p, or from q with `significance_from: q`).
* Optional (`shannon.enabled`): Shannon entropy of mode fractions per condition with a percentile
  bootstrap CI over objects.
