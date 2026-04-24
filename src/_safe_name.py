"""Legacy shim — prefer ``ctx.utils._safe_name``.

Plan 001 phase R1 moved the real module to
``src/ctx/utils/_safe_name.py``. This shim stays as long as legacy
``from _safe_name import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from
``ctx.utils._safe_name``.
"""

from ctx.utils._safe_name import *  # noqa: F401, F403
# Underscore-prefixed names aren't exported by ``import *``; the
# helper (added in the vuln-0003 fix) and the constant sets must be
# re-exported explicitly so existing importers keep working.
from ctx.utils._safe_name import (  # noqa: F401
    _SOURCE_NAME_RE,
    _WINDOWS_RESERVED,
    _is_windows_reserved,
)
