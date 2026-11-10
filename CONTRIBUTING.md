# Contributing

Thank you for your interest in morphoAgent. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
By contributing, you agree that your contributions are licensed under the project license
(GPL-3.0-only, see [LICENSE](LICENSE)). Bugs and feature requests go to the issue tracker; pull requests
should pass `ruff check .` and `pytest` (the same checks run in CI).

## Coding standards

* Python >= 3.10 (except `src/morphoagent/pipeline/vampire_runner.py`, which must stay Python 3.8 / pandas 0.25
  compatible and import only the standard library, numpy, pandas and Pillow at module level).
* Type hints on public functions; NumPy-style docstrings that state units (px, px^2, grey levels).
* No parameter is hard-coded in logic: add it to the dataclass in `config.py` **and** to
  `configs/default.yaml` with a comment, and document it in `docs/configuration.md`.
  `tests/test_config.py` fails if the YAML and the dataclasses differ.
* Logging via the `morphoagent` logger; long loops use `common.progress` (tqdm, one updating line).
* A step never overwrites existing results unless `--overwrite` is given (`common.prepare_step_dir`).
* Every step writes `run_manifest.json` through `common.write_manifest`.
* Every plot goes through `plotting.save_figure` (300-dpi transparent PNG + SVG + `source_<name>.csv`
  with the plotted coordinates).
* All repository text is in English.

## Tests and lint

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Tests use synthetic images only (fixed seeds); Cellpose is replaced by a threshold stand-in, so
the suite runs without a GPU or model download. Never add real data to the repository.

## Browser page (`src/morphoagent/pipeline/ui/static`)

* `page.html`, `app.css` and `app.js` are inlined by `ui/build.py`; never reference external URLs
  (a test fails on `http(s)://` in them). Plain JavaScript, no build step, no libraries.
* Keep the curation logic between the `BEGIN curation-core` / `END curation-core` markers free of
  DOM access: the tests run it with `node` (skipped when `node` is not installed).
* Escape every dataset- or server-provided string before inserting it as HTML.
* The assistant must stay vendor-neutral (endpoint URL, model name, key from an environment
  variable) and must never apply a suggestion without an explicit user action.

## Agent tools (`src/morphoagent/mcp`)

* Implement the behaviour as a plain function in `stage_tools.py` (no MCP import there): JSON-ready
  arguments and return value, NumPy-style docstring with units, never writes input images, refuses
  to replace existing results unless `overwrite=True`.
* Register it in `pipeline_server.py` with typed, described arguments (`Annotated[..., Field(...)]`)
  and wrap the call in `_reported()` so anticipated errors reach the client with their message.
* Tool outputs that are not pipeline steps go to `<output_dir>/<agent.output_subdir>/<tool>_<hash>/`
  with a `run_manifest.json`.
* Test the function directly on synthetic data in `tests/test_mcp_tools.py` and update docs/mcp.md.

## Adding an enhancement preset

1. Add a parameter dataclass to `config.py` and a field for it in `EnhanceConfig`; add the name to
   `PRESET_NAMES`.
2. Implement `my_preset(gray, params) -> (uint8 image, info dict)` in `presets.py` and register it in
   `get_preset`.
3. Add the defaults to `configs/default.yaml`, a row to the preset table in `docs/pipeline.md`
   and the keys to `docs/configuration.md`.
4. Add a unit test on a synthetic image (shape, dtype, determinism).

## Before publishing: leak check

Run a text search over every file git would track (`git ls-files --others --exclude-standard`
plus tracked files) before each push. It must find:

* no absolute machine paths (home or mount folders, drive letters), user or person names,
  institution, project or dataset names, real data or results;
* no Hangul or other non-English repository text;
* no agent-vendor or assistant names, with these exceptions only: the agent-layer file names
  `CLAUDE.md` and `.claude/` (and their entries in `.gitignore`), and in `README.md` the single
  sentence of the section "Agent client" that names the client the agent layer was tested with;
* no assistant attribution lines in commit messages (e.g. `Co-Authored-By`).

## Commits

Small, logical commits with plain descriptive messages.
