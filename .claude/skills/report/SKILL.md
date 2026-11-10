---
name: report
description: Build the decision report - a 16:9 slide deck and a Markdown report with one slide / section per decision snapshot, in stage order. Use when the researcher asks for a report, a summary deck, or the record of decisions so far.
---

# Report

## When to use

The researcher asks for the report, or a stage sequence they asked for is finished. The report is
built only from snapshots, which are written when the researcher decides a card (or presses
Snapshot on the review page). Undecided proposals do not appear.

## Tool calls

1. `list_pending(config_path)` - if proposals are pending, tell the researcher they will not be in
   the report and ask whether to build it now anyway.
2. `build_report(config_path, out_dir=<folder>, prefix=None)` - or, in the shell,
   `morphoagent report -c <config> --out <folder>`. Existing reports are never overwritten; a new
   build gets the next `_c<N>` number.
3. Report from the result: the `.pptx` and `.md` paths, the slide count and the snapshot names.

## Stop conditions

* Stop and report if there are no snapshots (the tool fails with a message); explain that decisions
  on the review page create them.
* Do not edit the generated files; if the researcher wants changes, build again after the change
  (a new `_c<N>`).
