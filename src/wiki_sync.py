"""Legacy shim — prefer ``ctx.core.wiki.wiki_sync``.

Plan 001 phase R3 moved the real module to
``src/ctx/core/wiki/wiki_sync.py``. This shim stays as long as legacy
``from wiki_sync import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from
``ctx.core.wiki.wiki_sync``.
"""

from ctx.core.wiki.wiki_sync import *  # noqa: F401, F403
