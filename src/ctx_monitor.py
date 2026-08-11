"""Public ctx dashboard entrypoint.

The dashboard implementation lives under :mod:`ctx.monitor`.  This flat module
stays as the console-script and backwards-compatible import surface.
"""

from __future__ import annotations

import sys
from typing import Any

from ctx.monitor import compat as _compat


if __name__ == "__main__":
    sys.exit(_compat.main())


def __getattr__(name: str) -> Any:
    return getattr(_compat, name)


sys.modules[__name__] = _compat
