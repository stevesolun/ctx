"""Legacy shim — prefer ``ctx.core.resolve.stack_skill_map``.

Plan 001 phase R2 moved the real module to
``src/ctx/core/resolve/stack_skill_map.py``. This shim stays as long as
legacy ``from stack_skill_map import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.core.resolve.stack_skill_map``.
"""

from ctx.core.resolve.stack_skill_map import *  # noqa: F401, F403
