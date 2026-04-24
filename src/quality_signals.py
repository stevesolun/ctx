"""Legacy shim — prefer ``ctx.core.quality.quality_signals``.

Plan 001 phase R2 moved the real module to
``src/ctx/core/quality/quality_signals.py``. This shim stays as long as
legacy ``from quality_signals import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.quality.quality_signals``.
"""

from ctx.core.quality.quality_signals import *  # noqa: F401, F403
