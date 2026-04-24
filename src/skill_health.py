"""Legacy shim — prefer ``ctx.adapters.claude_code.skill_health``.

Plan 001 phase R4c moved the real module to
``src/ctx/adapters/claude_code/skill_health.py``. This shim stays as long
as legacy ``from skill_health import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.adapters.claude_code.skill_health``.
"""

from ctx.adapters.claude_code.skill_health import *  # noqa: F401, F403
