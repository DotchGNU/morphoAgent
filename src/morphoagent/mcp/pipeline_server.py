"""MCP server exposing pipeline stages 0-3 and the decision protocol as typed tools (stdio transport).

Run it with ``morphoagent-mcp`` or ``python -m morphoagent.mcp.pipeline_server``.
The tools are thin, typed wrappers of :mod:`morphoagent.mcp.stage_tools`; see
that module for the full behaviour. All tools return a JSON object with the
paths of the files they produced and summary numbers read back from them.

Tools never modify input images, and never replace existing results unless
called with ``overwrite=true``.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .. import __version__
from ..pipeline.common import MissingInputError, OutputExistsError
from ..pipeline.config import ConfigError
from ..records.protocol import STAGES, Evidence, OptionIn, Provenance
from . import record_tools, stage_tools

SERVER_NAME = "morphoagent-pipeline"

INSTRUCTIONS = (
    "Bright-field cell morphology pipeline. Stages: 0 inspect_folder (read-only description of an image folder "
    "and a starting configuration block), 1 preview_enhance (display enhancement presets on sample images), "
    "2 run_segment (Cellpose-SAM parameter grid or full segmentation) and cut_distributions (cells left per "
    "condition for candidate area / frame-contact cut-offs), 3 run_vampire (VAMPIRE shape modes and elbow). "
    "Tools 1-3 read a YAML configuration file; changing a decision means editing that file. "
    "Researcher in the loop: after a stage, call propose (candidate options with figures, tables, numbers, a "
    "recommendation and its rationale); the researcher decides on the Decisions tab of the review page; read the "
    "result with get_decision. Later stage tools refuse to run while an earlier proposal is undecided. "
    "build_report turns the decision snapshots into a slide deck and a Markdown report. "
    "Quote numbers only from tool results. Existing results are kept unless overwrite=true is passed."
)

ConfigPath = Annotated[str, Field(description="Path of the YAML configuration file")]
Overwrite = Annotated[bool, Field(description="Replace existing results of this step (default false)")]

# Anticipated failures (missing inputs, existing outputs, invalid values, a failed external
# program) are reported to the client with their message; anything else is treated as a crash.
EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    OutputExistsError, MissingInputError, ConfigError, FileNotFoundError, NotADirectoryError, ValueError, RuntimeError,
)


@contextmanager
def _reported() -> Iterator[None]:
    """Re-raise anticipated pipeline errors as :class:`ToolError` so the caller sees the message."""
    try:
        yield
    except EXPECTED_ERRORS as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


server: MCPServer = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)


@server.tool(
    title="Inspect an image folder (stage 0)",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
def inspect_folder(
    path: Annotated[str, Field(description="Folder of single-channel bright-field images")],
    name_regex: Annotated[
        str | None, Field(description="Regex with named groups 'condition' and 'field' (default: the pipeline default)")
    ] = None,
    strip_prefix: Annotated[str, Field(description="Removed from the start of each file stem before parsing")] = "",
    strip_suffix: Annotated[str, Field(description="Removed from the end of each file stem before parsing")] = "",
    extensions: Annotated[list[str] | None, Field(description="Image file extensions (default: pipeline default)")] = None,
    read_pixels: Annotated[bool, Field(description="Decode every image for size, bit depth and grey levels")] = True,
) -> dict[str, Any]:
    """Describe an image folder (conditions, image counts, sizes, grey levels) without writing anything."""
    with _reported():
        return stage_tools.inspect_folder(path, name_regex, strip_prefix, strip_suffix, extensions, read_pixels)


@server.tool(
    title="Preview enhancement presets (stage 1)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
)
def preview_enhance(
    config_path: ConfigPath,
    presets: Annotated[list[str] | None, Field(description="Presets to compare (default: enhance.preview_presets)")] = None,
    samples: Annotated[list[str] | None, Field(description="Image ids <condition>_<field> (default: from config)")] = None,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Apply enhancement presets to sample images: contact sheet and edge / noise metrics (display only)."""
    with _reported():
        return stage_tools.preview_enhance(config_path, presets, samples, overwrite)


@server.tool(
    title="Segment with Cellpose-SAM (stage 2)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
)
def run_segment(
    config_path: ConfigPath,
    mode: Annotated[
        Literal["tune", "all"], Field(description="'tune' = parameter grid on sample images; 'all' = every image")
    ] = "tune",
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Cellpose-SAM segmentation: outline montages per setting (tune) or label images and measurements (all)."""
    with _reported():
        return stage_tools.run_segment(config_path, mode, overwrite)


@server.tool(
    title="Cells left for candidate cut-offs (stage 2)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
def cut_distributions(
    config_path: ConfigPath,
    area_min_px: Annotated[
        list[float] | None, Field(description="Area minima to test, px^2 (default: factors x select.area_min_px)")
    ] = None,
    frame_contact_px: Annotated[
        list[float] | None, Field(description="Frame-contact maxima to test, px (default: factors x select.frame_contact_px)")
    ] = None,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Count the cells kept per condition for every (area minimum, frame-contact maximum) pair; histograms."""
    with _reported():
        return stage_tools.cut_distributions(config_path, area_min_px, frame_contact_px, overwrite)


@server.tool(
    title="VAMPIRE shape modes (stage 3)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
)
def run_vampire(
    config_path: ConfigPath,
    k: Annotated[list[int] | None, Field(description="Numbers of shape modes to fit, each >= 2 (default: vampire.k)")] = None,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Fit VAMPIRE shape modes on the kept cells; elbow suggestions and per-mode counts for each K."""
    with _reported():
        return stage_tools.run_vampire(config_path, k, overwrite)


Stage = Literal["consult", "contrast", "segment", "cut", "cluster", "literature"]
assert Stage.__args__ == STAGES, "keep the Stage literal in step with records.protocol.STAGES"


@server.tool(
    title="Propose options for the researcher to decide",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
def propose(
    config_path: ConfigPath,
    stage: Annotated[Stage, Field(description="Pipeline stage the decision belongs to")],
    title: Annotated[str, Field(description="Card title, e.g. 'Minimum cell area'")],
    options: Annotated[list[OptionIn], Field(description="Candidates (id, label, figures, table rows, numbers, references)")],
    recommended: Annotated[str | None, Field(description="Option id you recommend")] = None,
    rationale: Annotated[str, Field(description="Why you recommend it; quote numbers from tool results only")] = "",
    evidence: Annotated[list[Evidence] | None, Field(description="Statements with the file/tool they come from")] = None,
    provenance: Annotated[
        Provenance | None, Field(description="Tool and parameters behind the candidates (default: read from the "
                                             "run_manifest.json next to the first figure)")] = None,
    main_figure: Annotated[str | None, Field(description="Figure path (one of the option figures) used for snapshots")] = None,
    figure_state: Annotated[dict[str, Any] | None, Field(description="Figure settings stored with snapshots")] = None,
) -> dict[str, Any]:
    """Write proposals/<stage>_<id>.json (figures copied next to it) for a decision card on the review page."""
    with _reported():
        return record_tools.propose(config_path, stage, title, options, recommended, rationale, evidence, provenance,
                                    main_figure, figure_state)


@server.tool(
    title="List proposals waiting for a decision",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
def list_pending(config_path: ConfigPath) -> dict[str, Any]:
    """Undecided proposals (id, stage, title, created time, recommended option, option ids and labels)."""
    with _reported():
        return record_tools.list_pending(config_path)


@server.tool(
    title="Read the researcher's decision on a proposal",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
)
def get_decision(
    config_path: ConfigPath,
    proposal_id: Annotated[str, Field(description="Id returned by propose")],
) -> dict[str, Any]:
    """The decision (chosen option id or 'rejected', comment, time); decision is null until decided."""
    with _reported():
        return record_tools.get_decision(config_path, proposal_id)


@server.tool(
    title="Build the decision report (slides + Markdown)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
def build_report(
    config_path: ConfigPath,
    out_dir: Annotated[str, Field(description="Output folder; existing reports are never overwritten")],
    prefix: Annotated[str | None, Field(description="File-name prefix (default: agent.report.prefix)")] = None,
) -> dict[str, Any]:
    """One slide and one Markdown section per snapshot, in stage and time order."""
    with _reported():
        return record_tools.build_report(config_path, out_dir, prefix)


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``morphoagent-mcp`` console script (stdio transport)."""
    p = argparse.ArgumentParser(prog="morphoagent-mcp", description="MCP server for the morphoagent pipeline (stdio).")
    p.add_argument("--version", action="version", version=f"morphoagent-mcp {__version__}")
    p.parse_args(argv)
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
