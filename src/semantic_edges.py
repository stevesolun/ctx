"""Legacy shim — prefer ``ctx.core.graph.semantic_edges``.

Plan 001 phase R2 moved the real module to
``src/ctx/core/graph/semantic_edges.py``. This shim stays as long as
legacy ``from semantic_edges import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.graph.semantic_edges``.
"""

from ctx.core.graph.semantic_edges import *  # noqa: F401, F403
