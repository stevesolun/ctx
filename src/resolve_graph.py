"""Legacy shim — prefer ``ctx.core.graph.resolve_graph``.

Plan 001 phase R2 moved the real module to
``src/ctx/core/graph/resolve_graph.py``. This shim stays as long as
legacy ``from resolve_graph import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.graph.resolve_graph``.
"""

from ctx.core.graph.resolve_graph import *  # noqa: F401, F403
# Module-level objects that pattern-based importers rely on (e.g.
# test patches against ``resolve_graph.GRAPH_PATH``); re-export
# explicitly so they survive the shim.
from ctx.core.graph.resolve_graph import (  # noqa: F401
    GRAPH_PATH,
    WIKI_DIR,
)
