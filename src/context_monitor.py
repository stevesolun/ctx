"""Legacy shim — prefer ``ctx.adapters.claude_code.hooks.context_monitor``.

Plan 001 phase R4b moved the real module to
``src/ctx/adapters/claude_code/hooks/context_monitor.py``. This shim stays as long as legacy
``from context_monitor import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from ``ctx.adapters.claude_code.hooks.context_monitor``.
"""

from ctx.adapters.claude_code.hooks.context_monitor import *  # noqa: F401, F403
