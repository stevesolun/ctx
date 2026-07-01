#!/usr/bin/env bash
set -euo pipefail

# no-mistakes agents run in a stripped-down environment. Keep ctx validation fast
# by exposing the verified project Python toolchain and Codex-bundled ripgrep.
ctx_python_bin="${CTX_NO_MISTAKES_PYTHON_BIN:-/tmp/ctx-verify-venv/bin}"
codex_resources="${CTX_NO_MISTAKES_CODEX_RESOURCES:-/Applications/Codex.app/Contents/Resources}"
real_codex="${CTX_NO_MISTAKES_REAL_CODEX:-${codex_resources}/codex}"

export PATH="${ctx_python_bin}:${codex_resources}:${PATH}"
if [[ -x "${ctx_python_bin}/python" ]]; then
  export VIRTUAL_ENV="${VIRTUAL_ENV:-/tmp/ctx-verify-venv}"
fi
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PIP_DISABLE_PIP_VERSION_CHECK="${PIP_DISABLE_PIP_VERSION_CHECK:-1}"

exec "${real_codex}" "$@"
