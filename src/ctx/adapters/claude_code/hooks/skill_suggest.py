#!/usr/bin/env python3
"""
skill_suggest.py -- Backward-compat shim for bundle_orchestrator.

The real implementation moved to ``bundle_orchestrator.py`` because
the name "skill suggest" misrepresented the job -- this hook produces
a recommendation BUNDLE that may span skills, agents, and MCP servers
in any combination. The graph ranks across all three types uniformly;
whatever scores highest wins.

This module exists because existing ``~/.claude/settings.json`` hook
configs invoke ``python skill_suggest.py``. Renaming the file without
a shim would silently break those installs on the next
``ctx-init --hooks`` refresh.

New call sites should use ``bundle_orchestrator.py`` directly.
"""

from __future__ import annotations

from ctx.adapters.claude_code.hooks.bundle_orchestrator import (  # noqa: F401 -- re-exports for legacy imports
    PENDING_SKILLS,
    PENDING_UNLOAD,
    already_shown_this_session,
    main,
    mark_shown,
)

if __name__ == "__main__":
    main()
