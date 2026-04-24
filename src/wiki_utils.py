"""Legacy shim — prefer ``ctx.core.wiki.wiki_utils``.

Plan 001 phase R3 moved the real module to
``src/ctx/core/wiki/wiki_utils.py``. This shim stays as long as
legacy ``from wiki_utils import X`` call sites exist (scheduled to be
dropped at the end of R6). New code should import directly from
``ctx.core.wiki.wiki_utils``.
"""

from ctx.core.wiki.wiki_utils import *  # noqa: F401, F403
# Underscore-prefixed helpers used by importers outside the wiki
# package must be re-exported explicitly since star-import excludes
# leading-underscore names. The private helper below was introduced
# in the Strix vuln-0003 fix.
from ctx.core.wiki.wiki_utils import (  # noqa: F401
    FRONTMATTER_RE,
    SAFE_NAME_RE,
)
