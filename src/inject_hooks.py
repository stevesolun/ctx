"""Legacy shim — prefer ``ctx.adapters.claude_code.inject_hooks``.

Plan 001 phase R4b moved the real module to
``src/ctx/adapters/claude_code/inject_hooks.py``. This shim stays as long as legacy
``from inject_hooks import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from ``ctx.adapters.claude_code.inject_hooks``.
"""

from ctx.adapters.claude_code.inject_hooks import *  # noqa: F401, F403
