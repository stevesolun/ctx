"""Legacy shim — prefer ``ctx.adapters.claude_code.install.skill_unload``.

Plan 001 phase R4a moved the real module to
``src/ctx/adapters/claude_code/install/skill_unload.py``. This shim stays
as long as legacy ``from skill_unload import X`` call sites exist
(scheduled to be dropped at the end of R6). New code should import
directly from ``ctx.adapters.claude_code.install.skill_unload``.
"""

from ctx.adapters.claude_code.install.skill_unload import *  # noqa: F401, F403
