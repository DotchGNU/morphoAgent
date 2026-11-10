"""Command-line interface: one subcommand per step plus ``run --from N --to M``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import __version__
from .common import LOG, MissingInputError, OutputExistsError, RunContext, setup_logging
from .config import Config, ConfigError, dump_default_config, load_config


def _steps() -> dict[int, tuple[str, Callable[[Config, RunContext], None], str]]:
    from .stats import run_stats
    from .steps_image import run_enhance, run_enhance_preview, run_load, run_segment, run_segment_tune
    from .steps_objects import run_curate, run_review, run_select
    from .vampire import run_vampire

    return {
        1: ("load", run_load, "list and validate images, parse condition/field, write the inventory"),
        2: ("enhance-preview", run_enhance_preview, "apply every enhancement preset to sample images"),
        3: ("enhance", run_enhance, "apply the chosen enhancement preset to all images"),
        4: ("segment-tune", run_segment_tune, "Cellpose-SAM parameter grid on sample images (outline montages)"),
        5: ("segment", run_segment, "Cellpose-SAM on all images; label TIFFs and measurements"),
        6: ("select", run_select, "rule-based object selection (largest piece, trim, area, frame contact)"),
        7: ("review", run_review, "numbered overlays and a review slide deck"),
        8: ("curate", run_curate, "optional manual delete/merge edits from a CSV"),
        9: ("vampire", run_vampire, "VAMPIRE shape modes, elbow, mode morphometry"),
        10: ("stats", run_stats, "object-level statistics and plots"),
    }


def build_parser() -> argparse.ArgumentParser:
    """Argument parser (exposed for tests and documentation)."""
    p = argparse.ArgumentParser(prog="morphoagent", description="Bright-field cell morphology pipeline.")
    p.add_argument("--version", action="version", version=f"morphoagent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--config", required=True, help="YAML configuration file")
        sp.add_argument("--overwrite", action="store_true", help="replace existing results of the step(s)")
        sp.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING (default: config log_level)")

    def curate_flags(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--yes", action="store_true", help="apply the curation CSV without asking")
        g.add_argument("--skip", action="store_true", help="skip curation without asking")
        sp.add_argument("--edits", default=None, help="curation CSV to apply (overrides curate.edits_csv)")

    for n, (name, _, helptext) in _steps().items():
        sp = sub.add_parser(name, help=f"step {n}: {helptext}")
        common(sp)
        if name == "curate":
            curate_flags(sp)
    rp = sub.add_parser("run", help="run a range of steps in order")
    common(rp)
    rp.add_argument("--from", dest="start", type=int, default=1, help="first step (1-10)")
    rp.add_argument("--to", dest="end", type=int, default=10, help="last step (1-10)")
    curate_flags(rp)
    ip = sub.add_parser("init-config", help="write the default configuration to a YAML file")
    ip.add_argument("path")
    up = sub.add_parser("ui", help="browser helper tools (offline review page, live server)")
    usub = up.add_subparsers(dest="ui_command", required=True)
    ub = usub.add_parser("build", help="write one self-contained offline HTML review page")
    ub.add_argument("-c", "--config", required=True, help="YAML configuration file")
    ub.add_argument("-o", "--out", default=None, help="output HTML file (default: ui.html_path or <output_dir>/ui/review.html)")
    ub.add_argument("--overwrite", action="store_true", help="replace an existing HTML file")
    ub.add_argument("--log-level", default=None)
    us = usub.add_parser("serve", help="serve the review page with autosave and live previews")
    us.add_argument("-c", "--config", required=True, help="YAML configuration file")
    us.add_argument("--host", default=None, help="bind address (default: ui.host); non-loopback needs a token")
    us.add_argument("--port", type=int, default=None, help="port (default: ui.port)")
    us.add_argument("--log-level", default=None)
    rep = sub.add_parser("report", help="slide deck and Markdown report from the decision snapshots (docs/protocol.md)")
    rep.add_argument("-c", "--config", required=True, help="YAML configuration file")
    rep.add_argument("--out", required=True, help="output folder; existing reports are never overwritten (c<N> increments)")
    rep.add_argument("--prefix", default=None, help="file-name prefix (default: agent.report.prefix)")
    rep.add_argument("--log-level", default=None)
    dp = sub.add_parser("decision", help="list pending proposals or import a decision file saved by the offline page")
    dp.add_argument("-c", "--config", required=True, help="YAML configuration file")
    dg = dp.add_mutually_exclusive_group(required=True)
    dg.add_argument("--list", action="store_true", help="print the pending proposals as JSON")
    dg.add_argument("--import", dest="import_file", default=None, help="decision JSON downloaded from the review page")
    dp.add_argument("--log-level", default=None)
    return p


def _records(args: argparse.Namespace, cfg: Config) -> int:
    import json

    from ..records import RecordsError, import_decision, list_pending
    from ..records.report import build_report

    try:
        if args.command == "report":
            r = build_report(cfg, args.out, args.prefix)
            print(json.dumps({k: r[k] for k in ("pptx", "markdown", "n_slides")}, indent=1))
        elif args.list:
            print(json.dumps(list_pending(cfg), indent=1))
        else:
            r = import_decision(cfg, args.import_file)
            print(json.dumps({k: r[k] for k in ("proposal_id", "choice", "path", "snapshot_dir")}, indent=1))
    except (RecordsError, MissingInputError, FileNotFoundError, FileExistsError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


def _ui(args: argparse.Namespace, cfg: Config) -> int:
    from .ui.build import run_ui_build
    from .ui.server import UIServerError, run_ui_serve

    try:
        if args.ui_command == "build":
            run_ui_build(cfg, args.out, args.overwrite)
        else:
            run_ui_serve(cfg, args.host, args.port)
    except (UIServerError, OutputExistsError, MissingInputError, OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``morphoagent`` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "init-config":
        from pathlib import Path

        if Path(args.path).exists():
            print(f"refusing to overwrite existing file {args.path}", file=sys.stderr)
            return 2
        dump_default_config(args.path)
        print(f"wrote {args.path}")
        return 0
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(args.log_level or cfg.log_level)
    if args.command == "ui":
        return _ui(args, cfg)
    if args.command in ("report", "decision"):
        return _records(args, cfg)
    if getattr(args, "edits", None):
        from pathlib import Path

        cfg.curate.edits_csv = str(Path(args.edits).resolve())
    answer = "yes" if getattr(args, "yes", False) else "skip" if getattr(args, "skip", False) else None
    ctx = RunContext(overwrite=args.overwrite, curate_answer=answer)
    steps = _steps()
    if args.command == "run":
        if not 1 <= args.start <= args.end <= 10:
            print("--from/--to must satisfy 1 <= from <= to <= 10", file=sys.stderr)
            return 2
        todo = list(range(args.start, args.end + 1))
    else:
        todo = [n for n, (name, _, _) in steps.items() if name == args.command]
    try:
        for n in todo:
            name, fn, _ = steps[n]
            LOG.info("=== step %d: %s ===", n, name)
            fn(cfg, ctx)
    except (OutputExistsError, MissingInputError, FileNotFoundError, ValueError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
