---
name: cluster
description: Stage 3 shape modes. Use after the cut-off decision (and optional curation) - fit VAMPIRE shape modes for several K, read the elbow suggestions, compare mode mean shapes and per-mode counts across K, and propose K. Shape modes are data-driven clusters, not cell states.
---

# Stage 3 - shape modes (VAMPIRE K)

## When to use

The `cut` proposal has a decision and steps 6-8 have run (step 8 applied or skipped). VAMPIRE must
be configured: `vampire.src_path` (a VAMPIRE_open checkout) and `vampire.python` (its interpreter).
If they are placeholders, stop and ask the researcher for them.

## Tool calls

1. `list_pending(config_path)` - stop if any earlier proposal is pending.
2. `run_vampire(config_path, k=[...])` - fit several K in one run. Default candidates: the
   configured `vampire.k` plus its neighbours (e.g. `[3, 4, 5]` around the default 4), and the
   elbow suggestions once known. If `09_vampire` already exists, ask before passing
   `overwrite=true`.
3. Read back: `n_cells`, `input_tables_from_step`, `elbow.k_second_diff`, `elbow.k_kneedle`,
   `elbow.inertia_monotonic_decreasing`, `per_k` (objects per display mode, objects dropped by
   VAMPIRE), `figures` (mode mean contours per K, elbow plot), `tables`.
4. Optional, for composition per condition: `morphoagent stats -c <config>` (step 10) and read
   `10_stats/mode_composition_K*.csv`. Only after K candidates are fitted; it does not decide K.

## Judge K with the outputs

* Elbow: quote both suggestions and the inertia table (`elbow_inertia.csv`). They may disagree;
  say so.
* Mode mean shapes: does an additional mode add a visibly distinct mean outline, or split one
  shape into two near-identical ones? Name the figure files you looked at.
* Sensitivity: how cells move between modes from K to K+1 (`mode_mapping_K*.csv`); a mode holding
  very few cells (give n of `n_cells`) is unstable.
* Shape modes are clusters of outlines. Do not name them as cell states (for example
  "hypertrophic", "dying"); stage 5 hypotheses are not available.

## The card

`propose(stage="cluster", title="Number of shape modes", ...)`: one option per K; `figures` = that
K's mode mean-contour figure (and the elbow plot on one option); `table` = cells per mode;
`numbers` = K, `n_cells`, smallest mode n; `config_changes` = `{"vampire.k": [K]}`;
`recommended` + `rationale` from the elbow, mean shapes and smallest-mode size, each with its file.

## Stop conditions

* Stop after `propose`; wait for `get_decision`, then write `vampire.k` and run
  `morphoagent stats -c <config>` only if the researcher asks for the statistics.
* Stop and report if VAMPIRE fails (`09_vampire/vampire_work/runner.log`) or drops many objects.
