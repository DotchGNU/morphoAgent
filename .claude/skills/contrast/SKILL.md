---
name: contrast
description: Stage 1 display enhancement. Use after the consult proposal is decided, when the researcher wants to choose how images are shown in review pages, overlays and figures. Runs preview_enhance on sample images, diagnoses the images with the returned metrics, and proposes an enhancement preset. Display only - never changes segmentation or measurements.
---

# Stage 1 - contrast

## When to use

The `consult` proposal has a decision and the configuration file exists. Enhancement affects only
the images used for display (review overlays, figures); segmentation and measurements always use
the raw images. Say this to the researcher once.

## Tool calls

1. `list_pending(config_path)` - stop if a `consult` proposal is pending.
2. `preview_enhance(config_path)` - all presets on the default samples (first image of the first
   4 conditions). Use `samples=[...]` only if the researcher names images.
3. Read back from the result: `per_preset` (mean `edge_strength`, `flat_noise`, `p50` per preset),
   `sample_images`, `default_preset`, `contact_sheet` (and `preview_metrics_csv` for per-image
   values). Look at the contact sheet image.

## Diagnose with numbers

* `edge_strength`: higher = crisper edges. `flat_noise`: lower = cleaner background. `p50`: median
  grey level after the preset.
* Compare presets on the same samples; quote the per-preset means and the spread across samples
  from `preview_metrics.csv` (state n samples). A sample whose `p50` or `flat_noise` differs
  strongly from the others is worth naming (e.g. a field at a plate edge).

Presets available: `figure-default` (default), `tone-unsharp`, `clahe`, `none`.

## Optional display modules - beta, not implemented yet

Mention these only as future options; do not run or promise them:

| Module | Known pitfall |
|---|---|
| denoise | can over-smooth fine edges such as thin protrusions |
| deblur | can create ringing around high-contrast edges |
| flat-field correction | can fail on dense fields where no background is left to estimate |
| per-field histogram matching | tile or field normalisation can create blotchy backgrounds |

All display corrections, present or future, never change measurements.

## The card

`propose(config_path, stage="contrast", title="Display enhancement", ...)`:

* One option per preset compared. `figures` = `[contact_sheet]` for each option (the sheet shows
  all presets; its `.svg` and `source_contact_sheet.csv` are copied automatically). `numbers` = that
  preset's mean `edge_strength`, `flat_noise`, `p50`. `config_changes` = `{"enhance.preset": "<name>"}`.
* `recommended` = the preset with the best edge / noise balance by the numbers; `rationale` quotes
  them and names the trade-off.
* `provenance` is read from the step manifest automatically.

## Stop conditions

* Stop after `propose` and wait for the decision (`get_decision`). After it, write
  `enhance.preset` (and any parameter the researcher set on the review page) to the config.
* The full enhancement step (`morphoagent enhance -c <config>`, step 3) runs only after the
  decision; do not run it before.
* If the researcher wants a re-run with other samples, use a new samples list; pass
  `overwrite=true` only when they ask to replace the existing preview.
