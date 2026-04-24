"""Legacy shim — prefer ``ctx.adapters.claude_code.hooks.skill_suggest``.

Plan 001 phase R4b moved the real module to
``src/ctx/adapters/claude_code/hooks/skill_suggest.py``. This shim stays as long as legacy
``from skill_suggest import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from ``ctx.adapters.claude_code.hooks.skill_suggest``.
"""

from ctx.adapters.claude_code.hooks.skill_suggest import *  # noqa: F401, F403
