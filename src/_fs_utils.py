"""Legacy shim — prefer ``ctx.utils._fs_utils``.

Plan 001 phase R1 moved the real module to
``src/ctx/utils/_fs_utils.py``. This shim stays as long as legacy
``from _fs_utils import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from
``ctx.utils._fs_utils``.
"""

from ctx.utils._fs_utils import *  # noqa: F401, F403
# Underscore-prefixed helpers used by semantic_edges must be re-exported
# explicitly since ``import *`` excludes leading-underscore names.
from ctx.utils._fs_utils import (  # noqa: F401
    _replace_with_retry,
)
