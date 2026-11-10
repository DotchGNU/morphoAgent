"""MCP server for literature look-ups: PubMed / Europe PMC search, abstracts, methods text, citation checks.

Run it with ``morphoagent-literature [--config FILE]`` or
``python -m morphoagent.mcp.literature_server``. Settings come from the
``literature`` section of the configuration file (defaults without one); the
optional NCBI e-mail and API key come only from that file or the environment
(docs/literature.md). The tools are thin wrappers of
:class:`morphoagent.literature.LiteratureClient`.

All tools are read-only towards the researcher's data; they only add entries
to the response cache (``literature.cache_dir``).
"""

from __future__ import annotations

import argparse
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .. import __version__
from ..literature import LiteratureClient, LiteratureError
from ..pipeline.common import setup_logging
from ..pipeline.config import ConfigError, load_config

SERVER_NAME = "morphoagent-literature"
#: Environment variable naming a configuration file (used when ``--config`` is not given).
CONFIG_ENV = "MORPHOAGENT_CONFIG"

INSTRUCTIONS = (
    "Literature look-ups for stage 4 (literature-based revision suggestions). search_literature finds candidate "
    "papers (PubMed or Europe PMC); fetch_abstract returns the abstract of one PMID; fetch_methods_text returns the "
    "methods paragraphs of an open-access article by PMC id; verify_citation checks that a PMID / DOI / title exist "
    "and belong to the same paper. Cite a paper only after verify_citation returned verified=true, and quote the "
    "supporting sentence verbatim from fetch_abstract or fetch_methods_text. If no verified sentence supports a "
    "claim, say that no literature support was found."
)

EXPECTED_ERRORS: tuple[type[Exception], ...] = (LiteratureError, ValueError, ConfigError)


@contextmanager
def _reported() -> Iterator[None]:
    """Re-raise anticipated errors as :class:`ToolError` so the caller sees the message."""
    try:
        yield
    except EXPECTED_ERRORS as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


_client: LiteratureClient | None = None
_config_path: str | None = None
_lock = threading.Lock()


def configure(config_path: str | None) -> None:
    """Use the ``literature`` section of ``config_path`` (``None`` = defaults) for later tool calls."""
    global _client, _config_path
    with _lock:
        _config_path = config_path
        _client = None


def client() -> LiteratureClient:
    """The shared client, created on first use from the configured file."""
    global _client
    with _lock:
        if _client is None:
            cfg = load_config(_config_path)
            setup_logging(cfg.log_level)
            _client = LiteratureClient(cfg.literature)
        return _client


server: MCPServer = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)
_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


@server.tool(title="Search PubMed or Europe PMC", annotations=_READ)
def search_literature(
    query: Annotated[str, Field(description="Query in PubMed / Europe PMC syntax")],
    max_results: Annotated[int, Field(description="Number of records to return (1 .. literature.max_results)")] = 10,
    source: Annotated[Literal["pubmed", "europepmc"], Field(description="Service to search")] = "pubmed",
) -> dict[str, Any]:
    """Candidate papers: pmid, doi, title, journal, year, authors (first 3 + et al.), pmcid."""
    with _reported():
        return client().search_literature(query, max_results, source)


@server.tool(title="Fetch a PubMed abstract", annotations=_READ)
def fetch_abstract(
    pmid: Annotated[str, Field(description="PubMed id (digits)")],
) -> dict[str, Any]:
    """Title, abstract (structured labels kept), journal, year, DOI and PMC id of one PubMed record."""
    with _reported():
        return client().fetch_abstract(pmid)


@server.tool(title="Fetch methods paragraphs (open access)", annotations=_READ)
def fetch_methods_text(
    pmcid: Annotated[str, Field(description="PubMed Central id, e.g. PMC1234567 (from fetch_abstract or search)")],
) -> dict[str, Any]:
    """Methods-section paragraphs of an open-access article from Europe PMC full text (best effort)."""
    with _reported():
        return client().fetch_methods_text(pmcid)


@server.tool(title="Verify a citation", annotations=_READ)
def verify_citation(
    pmid: Annotated[str | None, Field(description="PubMed id to check")] = None,
    doi: Annotated[str | None, Field(description="DOI to check (10.x/...)")] = None,
    title: Annotated[str | None, Field(description="Title to compare with the record")] = None,
) -> dict[str, Any]:
    """Existence of the PMID / DOI, whether they point to the same paper, and title similarity; verified flag."""
    with _reported():
        return client().verify_citation(pmid, doi, title)


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``morphoagent-literature`` console script (stdio transport)."""
    p = argparse.ArgumentParser(prog="morphoagent-literature",
                                description="MCP server for literature look-ups (PubMed, Europe PMC; stdio).")
    p.add_argument("--version", action="version", version=f"morphoagent-literature {__version__}")
    p.add_argument("-c", "--config", default=None,
                   help=f"YAML configuration whose 'literature' section is used (default: ${CONFIG_ENV} or defaults)")
    args = p.parse_args(argv)
    configure(args.config or os.environ.get(CONFIG_ENV) or None)
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
