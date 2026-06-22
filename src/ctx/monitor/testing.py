"""Stable test facade for monitor compatibility helpers.

Runtime code should import the extracted monitor modules directly. Tests that
still need legacy wiring can use this facade without depending on private
``compat._*`` names.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from ctx.monitor import compat as _compat


def _compat_name(name: str) -> str:
    if name.startswith("_") and not name.startswith("__"):
        raise AttributeError(
            f"ctx.monitor.testing exposes public helper names only: {name}",
        )
    private_name = f"_{name}"
    if hasattr(_compat, private_name):
        return private_name
    return name


def __getattr__(name: str) -> Any:
    return getattr(_compat, _compat_name(name))


class _MonitorTestingModule(ModuleType):
    def __getattr__(self, name: str) -> Any:
        return getattr(_compat, _compat_name(name))

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__"):
            super().__setattr__(name, value)
            return
        compat_name = _compat_name(name)
        if hasattr(_compat, compat_name):
            setattr(_compat, compat_name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        compat_name = _compat_name(name)
        if hasattr(_compat, compat_name):
            delattr(_compat, compat_name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _MonitorTestingModule
