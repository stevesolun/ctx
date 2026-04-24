"""Legacy shim — prefer ``ctx.adapters.claude_code.install.install_utils``.

Plan 001 phase R4a moved the real module to
``src/ctx/adapters/claude_code/install/install_utils.py``. This shim stays
as long as legacy ``from install_utils import X`` call sites exist
(scheduled to be dropped at the end of R6). New code should import
directly from ``ctx.adapters.claude_code.install.install_utils``.
"""

from ctx.adapters.claude_code.install.install_utils import *  # noqa: F401, F403
