---
name: consult
description: Stage 0 intake for a new morphology analysis. Use when the researcher starts a new image set or asks to set up an analysis - find the image folder, parse file names into conditions, agree on the control group, pixel size (um/px) and replicate unit, and write the configuration file. Ends with a consult proposal the researcher decides.
---

# Stage 0 - consult

## When to use

The researcher points at an image folder, or no configuration file exists yet for the analysis.
Nothing else can run before this stage is decided.

## Ask first (one short list, in the researcher's language)

1. Image folder path, and where outputs may be written (a new, empty folder).
2. Which part of the file name is the condition and which is the field (or let `inspect_folder`
   try the default pattern first).
3. Control (reference) group.
4. Pixel size in um/px (objective / camera), or "unknown" (results then stay in px).
5. Replicate unit: what is independent - fields, wells, coverslips, animals, experiment days? The
   pipeline's statistics are object-level; state this to the researcher if their replicate unit is
   larger than a field.

Do not guess answers; leave unknowns as unknown and say so in the proposal.

## Tool calls

1. `inspect_folder(path=<folder>)` - default `name_regex` `^(?P<condition>.+)_(?P<field>Image\d+)$`.
   If `n_unmatched > 0`, show `unmatched_examples` and retry with `strip_prefix`, `strip_suffix` or
   a `name_regex` with groups `condition` and `field`. Repeat until every image matches or the
   researcher accepts the unmatched files being skipped.
2. Report from the result only: `n_files`, `n_matched` of `n_files`, images per condition
   (`conditions`), `image_sizes`, `pixel_modes`, `all_zero_files`, `duplicate_image_ids`,
   `grey_levels`, `warnings`.
3. Write the configuration (shell): `morphoagent init-config <config.yaml>`, then edit only these
   keys: `output_dir`, the `input` block returned by `inspect_folder`, `conditions.order`,
   `conditions.reference`, and the pixel size if known: `stats.pixel_size_um`, or, when the researcher gives
   a field width, `stats.pixel_calibration` (`field_width_um`, `image_width_px`, `binning`) so the value is
   derived exactly instead of rounded. Do not change any other default.
4. `propose(config_path=<config.yaml>, stage="consult", title="Analysis set-up", ...)`.

## The card

* One option per plausible set-up (usually one, sometimes alternatives such as two different
  control groups or two parsing patterns). Each option's `table` = conditions with image counts
  from `inspect_folder`; `numbers` = `n_matched`, `n_unmatched`, image width/height (px), pixel size;
  `config_changes` = the keys you wrote or would write.
* `description`: replicate unit as stated by the researcher, and any unknowns.
* `rationale`: why the recommended set-up fits the folder (numbers quoted from `inspect_folder`).
* No figures are required.

## Stop conditions

* Stop after `propose`; tell the researcher to decide on the Decisions tab
  (`morphoagent ui serve -c <config.yaml>`, then http://127.0.0.1:8765/).
* Stop and ask if the folder has no images, if the parsed conditions do not match what the
  researcher described, or if images have different sizes or bit depths (`image_sizes`,
  `pixel_modes` with more than one entry).
