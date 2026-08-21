#!/usr/bin/env python3
"""Validate, hydrate, or refresh the strict graph release manifest."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "ctx" / "core" / "graph" / "release_artifacts.py"
MODULE_NAME = "_ctx_graph_release_artifacts_cli"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load graph release manifest resolver: {MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = MODULE
SPEC.loader.exec_module(MODULE)
main = MODULE.main


if __name__ == "__main__":
    raise SystemExit(main())
