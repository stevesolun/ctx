"""Registry of MCP catalog sources.

Each :class:`~mcp_sources.base.Source` implementation lives in
``mcp_sources/<name>.py`` and exposes a module-level ``SOURCE`` attribute.
This package imports every known source eagerly and maps its ``name`` into
:data:`SOURCES` so CLI dispatchers can resolve ``--source <name>`` without
reflection or entry-point discovery.

Adding a source is a two-line patch here plus the new module file; keep the
list alphabetical so reviewers can tell at a glance whether a source is
already registered.
"""

from mcp_sources.awesome_mcp import SOURCE as _AWESOME
from mcp_sources.base import Source
from mcp_sources.pulsemcp import SOURCE as _PULSEMCP

SOURCES: dict[str, Source] = {
    _AWESOME.name: _AWESOME,
    _PULSEMCP.name: _PULSEMCP,
}

__all__ = ["SOURCES", "Source"]
