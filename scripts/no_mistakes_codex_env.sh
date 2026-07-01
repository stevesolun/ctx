#!/usr/bin/env bash
set -euo pipefail

# no-mistakes agents run in a stripped-down environment. Keep ctx validation fast
# by exposing the verified project Python toolchain and Codex-bundled ripgrep.
default_ctx_python_bin="/tmp/ctx-verify-venv/bin"
ctx_python_bin="${CTX_NO_MISTAKES_PYTHON_BIN:-${default_ctx_python_bin}}"
codex_resources="${CTX_NO_MISTAKES_CODEX_RESOURCES:-/Applications/Codex.app/Contents/Resources}"
real_codex="${CTX_NO_MISTAKES_REAL_CODEX:-${codex_resources}/codex}"

is_trusted_python_bin() {
  local bin_dir="$1"
  local venv_dir="${bin_dir%/bin}"

  [[ -d "${bin_dir}" && -x "${bin_dir}/python" ]] || return 1
  [[ -O "${venv_dir}" && -O "${bin_dir}" ]] || return 1
  [[ -z "$(find "${venv_dir}" "${bin_dir}" -prune -perm -022 -print -quit)" ]]
}

trusted_ctx_python_bin=""
if [[ -n "${CTX_NO_MISTAKES_PYTHON_BIN:-}" ]]; then
  trusted_ctx_python_bin="${ctx_python_bin}"
elif is_trusted_python_bin "${ctx_python_bin}"; then
  trusted_ctx_python_bin="${ctx_python_bin}"
fi

if [[ -n "${trusted_ctx_python_bin}" ]]; then
  export PATH="${trusted_ctx_python_bin}:${codex_resources}:${PATH}"
  if [[ -x "${trusted_ctx_python_bin}/python" ]]; then
    export VIRTUAL_ENV="${VIRTUAL_ENV:-${trusted_ctx_python_bin%/bin}}"
  fi
else
  export PATH="${codex_resources}:${PATH}"
fi
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PIP_DISABLE_PIP_VERSION_CHECK="${PIP_DISABLE_PIP_VERSION_CHECK:-1}"

exec "${real_codex}" "$@"
