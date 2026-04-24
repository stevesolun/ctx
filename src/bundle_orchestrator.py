"""Legacy shim — prefer ``ctx.adapters.claude_code.hooks.bundle_orchestrator``.

Plan 001 phase R4b moved the real module to
``src/ctx/adapters/claude_code/hooks/bundle_orchestrator.py``. This shim stays as long as legacy
``from bundle_orchestrator import X`` call sites exist (scheduled to be dropped
at the end of R6). New code should import directly from ``ctx.adapters.claude_code.hooks.bundle_orchestrator``.
"""

from ctx.adapters.claude_code.hooks.bundle_orchestrator import *  # noqa: F401, F403
