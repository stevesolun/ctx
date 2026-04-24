"""Legacy shim — prefer ``ctx.utils._file_lock``.

Plan 001 phase R1 moved the real module to
``src/ctx/utils/_file_lock.py``. This shim stays as long as legacy
``from _file_lock import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from
``ctx.utils._file_lock``.
"""

from ctx.utils._file_lock import *  # noqa: F401, F403
