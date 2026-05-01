#!/usr/bin/env bash
# Compatibility wrapper for source checkouts.
#
# The supported installer is ctx-init. This script exists only for users who
# still run `bash install.sh` from a clone; it delegates to the packaged
# bootstrap module instead of calling legacy flat scripts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX_DIR="$SCRIPT_DIR"

if [[ "${1:-}" == "--ctx-dir" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "install.sh: --ctx-dir requires a path" >&2
    exit 2
  fi
  CTX_DIR="$2"
  shift 2
elif [[ -n "${1:-}" && "${1:-}" != --* ]]; then
  CTX_DIR="$1"
  shift
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON="python"
fi

export PYTHONPATH="$CTX_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m ctx_init "$@"
