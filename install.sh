#!/usr/bin/env bash
# install.sh -- Deploy the Alive Skill System to ~/.claude/
#
# What this does:
#   1. Initializes the skill wiki at ~/.claude/skill-wiki/
#   2. Builds a bulk skill catalog (catalog.md) from all installed skills
#   3. Deploys skill-router micro-skill to ~/.claude/agents/skill-router/
#   4. Injects PostToolUse + Stop hooks into ~/.claude/settings.json
#   5. Deploys Python helpers (context-monitor, usage-tracker, skill-compiler)
#   6. Creates skill-registry.json to track known skill directories
#
# Does NOT auto-convert skills to micro-skills (use skill-transformer.py separately)
# Does NOT create individual wiki pages for all 1489 skills (done on demand)
#
# Usage:
#   bash install.sh [--ctx-dir /path/to/ctx]

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_DIR="$HOME/.claude"
WIKI_DIR="$CLAUDE_DIR/skill-wiki"
AGENTS_DIR="$CLAUDE_DIR/agents"
SKILLS_DIR="$CLAUDE_DIR/skills"

# Resolve ctx/ dir (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX_DIR="${1:-$SCRIPT_DIR}"
if [[ "$1" == "--ctx-dir" && -n "${2:-}" ]]; then
  CTX_DIR="$2"
fi

PYTHON="${PYTHON:-python3}"
# Windows: try 'python' if python3 not found
if ! command -v "$PYTHON" &>/dev/null; then
  PYTHON="python"
fi

log() { echo "[install] $*"; }
ok()  { echo "[install] ✓ $*"; }
warn(){ echo "[install] ⚠ $*"; }

# ── Step 1: Initialize wiki ───────────────────────────────────────────────────
log "Step 1: Initializing skill wiki at $WIKI_DIR"
"$PYTHON" "$CTX_DIR/wiki_sync.py" --init --wiki "$WIKI_DIR"
ok "Wiki initialized"

# ── Step 2: Build bulk skill catalog ─────────────────────────────────────────
log "Step 2: Building skill catalog (all installed skills → catalog.md)"
"$PYTHON" "$CTX_DIR/catalog_builder.py" \
  --wiki "$WIKI_DIR" \
  --skills-dir "$SKILLS_DIR" \
  --agents-dir "$AGENTS_DIR"
ok "Catalog built"

# ── Step 3: Deploy skill-router micro-skill ───────────────────────────────────
ROUTER_SRC="$CTX_DIR/skills/skill-router"
ROUTER_DST="$AGENTS_DIR/skill-router"

if [[ -d "$ROUTER_SRC" ]]; then
  log "Step 3: Deploying skill-router to $ROUTER_DST"
  mkdir -p "$ROUTER_DST"
  cp -r "$ROUTER_SRC/." "$ROUTER_DST/"
  ok "skill-router deployed"
else
  warn "skills/skill-router/ not found in $CTX_DIR — skipping router deploy"
fi

# ── Step 4: Inject hooks into settings.json ───────────────────────────────────
log "Step 4: Injecting hooks into $CLAUDE_DIR/settings.json"
"$PYTHON" "$CTX_DIR/inject_hooks.py" \
  --settings "$CLAUDE_DIR/settings.json" \
  --ctx-dir "$CTX_DIR"
ok "Hooks injected"

# ── Step 5: Create skill-registry.json ───────────────────────────────────────
REGISTRY="$CLAUDE_DIR/skill-registry.json"
log "Step 5: Creating $REGISTRY"
"$PYTHON" - << PYEOF
import json, os
from pathlib import Path

registry_path = Path("$REGISTRY")
skills_dir = Path("$SKILLS_DIR")
agents_dir = Path("$AGENTS_DIR")

existing = {}
if registry_path.exists():
    try:
        existing = json.loads(registry_path.read_text())
    except Exception:
        existing = {}

dirs = existing.get("skill_dirs", [])

# Add default dirs if not already registered
defaults = [str(skills_dir), str(agents_dir)]
for d in defaults:
    if d not in dirs and Path(d).exists():
        dirs.append(d)

registry = {
    "version": 1,
    "skill_dirs": dirs,
    "wiki": "$WIKI_DIR",
    "ctx_dir": "$CTX_DIR",
    "updated": "$(date -u +%Y-%m-%d)",
}
registry_path.write_text(json.dumps(registry, indent=2))
print(f"Registry written: {len(dirs)} skill dirs")
PYEOF
ok "Skill registry created"

# ── Step 6: Copy Python helpers ───────────────────────────────────────────────
log "Step 6: Skill tools available at $CTX_DIR"
log "  context-monitor.py  → PostToolUse hook"
log "  usage-tracker.py    → Stop hook"
log "  skill-transformer.py   → convert skills >180 lines"
log "  catalog_builder.py  → rebuild skill catalog"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo " Alive Skill System installed successfully!"
echo "═══════════════════════════════════════════════════════"
echo " Wiki:         $WIKI_DIR"
echo " Registry:     $REGISTRY"
echo " Skill router: $ROUTER_DST"
echo ""
echo " To convert large skills interactively:"
echo "   $PYTHON $CTX_DIR/skill-transformer.py --scan $SKILLS_DIR"
echo ""
echo " To rebuild catalog after adding new skills:"
echo "   $PYTHON $CTX_DIR/catalog_builder.py --wiki $WIKI_DIR"
echo ""
