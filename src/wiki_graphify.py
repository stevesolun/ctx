"""Legacy shim — prefer ``ctx.core.wiki.wiki_graphify``.

Plan 001 phase R3 moved the real module to
``src/ctx/core/wiki/wiki_graphify.py``. This shim stays as long as
legacy ``from wiki_graphify import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.wiki.wiki_graphify``.
"""

from ctx.core.wiki.wiki_graphify import *  # noqa: F401, F403
