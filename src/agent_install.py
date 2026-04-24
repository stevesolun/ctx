"""Legacy shim — prefer ``ctx.adapters.claude_code.install.agent_install``.

Plan 001 phase R4a moved the real module to
``src/ctx/adapters/claude_code/install/agent_install.py``. This shim stays
as long as legacy ``from agent_install import X`` call sites exist
(scheduled to be dropped at the end of R6). New code should import
directly from ``ctx.adapters.claude_code.install.agent_install``.
"""

from ctx.adapters.claude_code.install.agent_install import *  # noqa: F401, F403
