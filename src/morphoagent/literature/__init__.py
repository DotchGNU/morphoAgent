"""Literature look-ups (PubMed E-utilities, Europe PMC) used by the ``morphoagent-literature`` tools.

The functions here do not import the MCP SDK; :mod:`morphoagent.mcp.literature_server`
registers them as tools. See docs/literature.md.
"""

from .client import LiteratureClient
from .http import CachedHttp, LiteratureError, NotFoundError, default_cache_dir

__all__ = ["CachedHttp", "LiteratureClient", "LiteratureError", "NotFoundError", "default_cache_dir"]
