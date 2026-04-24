"""Legacy shim — prefer ``ctx.adapters.claude_code.skill_loader``.

Plan 001 phase R4c moved the real module to
``src/ctx/adapters/claude_code/skill_loader.py``. This shim stays as long
as legacy ``from skill_loader import X`` call sites exist (scheduled to
be dropped at the end of R6). New code should import directly from
``ctx.adapters.claude_code.skill_loader``.
"""

from ctx.adapters.claude_code.skill_loader import *  # noqa: F401, F403
