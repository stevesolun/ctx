"""Legacy shim — prefer ``ctx.core.resolve.resolve_skills``.

Plan 001 phase R2 moved the real module to
``src/ctx/core/resolve/resolve_skills.py``. This shim stays as long as
legacy ``from resolve_skills import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.resolve.resolve_skills``.
"""

from ctx.core.resolve.resolve_skills import *  # noqa: F401, F403
