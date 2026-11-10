"""Agent-facing wrappers of the proposal / decision protocol (no MCP import).

Each function takes the path of the YAML configuration (which locates the
records folder) and returns JSON-serialisable values. See docs/protocol.md.
"""

from __future__ import annotations

from typing import Any

from ..pipeline.common import setup_logging
from ..pipeline.config import Config, load_config
from ..records import protocol
from ..records.report import build_report as _build_report


def _load(config_path: str) -> Config:
    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    return cfg


def propose(
    config_path: str,
    stage: str,
    title: str,
    options: list[Any],
    recommended: str | None = None,
    rationale: str = "",
    evidence: list[Any] | None = None,
    provenance: Any = None,
    main_figure: str | None = None,
    figure_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a proposal; see :func:`morphoagent.records.protocol.propose`."""
    return protocol.propose(_load(config_path), stage, title, options, recommended, rationale, evidence, provenance,
                            main_figure, figure_state)


def list_pending(config_path: str) -> dict[str, Any]:
    """Undecided proposals: ``{"n_pending": int, "pending": [...]}``."""
    pending = protocol.list_pending(_load(config_path))
    return {"n_pending": len(pending), "pending": pending}


def get_decision(config_path: str, proposal_id: str) -> dict[str, Any]:
    """``{"proposal_id", "decided", "decision"}``; ``decision`` is ``None`` until the researcher decides."""
    dec = protocol.get_decision(_load(config_path), proposal_id)
    return {"proposal_id": proposal_id, "decided": dec is not None, "decision": dec}


def build_report(config_path: str, out_dir: str, prefix: str | None = None) -> dict[str, Any]:
    """Slide deck and Markdown report from the snapshots; see :func:`morphoagent.records.report.build_report`."""
    return _build_report(_load(config_path), out_dir, prefix)
