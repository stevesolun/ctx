#!/usr/bin/env bash
set -euo pipefail

# no-mistakes agents run in a stripped-down environment. Keep ctx validation fast
# by exposing the verified project Python toolchain and Codex-bundled ripgrep.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"
pwd_ctx_python_bin="${PWD}/.venv/bin"
repo_ctx_python_bin="${repo_root}/.venv/bin"
fallback_ctx_python_bin="/tmp/ctx-verify-venv/bin"
wrapper_path="${script_dir}/$(basename -- "${BASH_SOURCE[0]}")"
default_codex_app_paths="/Applications/Codex.app/Contents/Resources/codex:/Applications/ChatGPT.app/Contents/Resources/codex:${HOME:-}/Applications/Codex.app/Contents/Resources/codex:${HOME:-}/Applications/ChatGPT.app/Contents/Resources/codex"

is_runnable_codex() {
  local candidate="$1"

  [[ -f "${candidate}" && -x "${candidate}" ]] || return 1
  [[ "${candidate}" -ef "${wrapper_path}" ]] && return 1
  return 0
}

resolve_real_codex() {
  local candidate
  local path_codex
  local codex_app_paths
  local codex_app_candidates=()

  if [[ -n "${CTX_NO_MISTAKES_REAL_CODEX:-}" ]]; then
    is_runnable_codex "${CTX_NO_MISTAKES_REAL_CODEX}" || {
      echo "Configured Codex executable is not runnable: ${CTX_NO_MISTAKES_REAL_CODEX}" >&2
      return 127
    }
    printf '%s\n' "${CTX_NO_MISTAKES_REAL_CODEX}"
    return 0
  fi

  if [[ -n "${CTX_NO_MISTAKES_CODEX_RESOURCES:-}" ]]; then
    candidate="${CTX_NO_MISTAKES_CODEX_RESOURCES}/codex"
    is_runnable_codex "${candidate}" || {
      echo "Configured Codex resources do not contain a runnable codex: ${candidate}" >&2
      return 127
    }
    printf '%s\n' "${candidate}"
    return 0
  fi

  codex_app_paths="${CTX_NO_MISTAKES_CODEX_APP_PATHS-${default_codex_app_paths}}"
  if [[ -n "${codex_app_paths}" ]]; then
    IFS=: read -r -a codex_app_candidates <<<"${codex_app_paths}"
    for candidate in "${codex_app_candidates[@]}"; do
      [[ -n "${candidate}" ]] || continue
      if is_runnable_codex "${candidate}"; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done
  fi

  path_codex="$(command -v codex 2>/dev/null || true)"
  if [[ -n "${path_codex}" ]] && is_runnable_codex "${path_codex}"; then
    printf '%s\n' "${path_codex}"
    return 0
  fi

  echo "Unable to find a runnable Codex executable; set CTX_NO_MISTAKES_REAL_CODEX." >&2
  return 127
}

real_codex="$(resolve_real_codex)"
codex_resources="${CTX_NO_MISTAKES_CODEX_RESOURCES:-$(dirname -- "${real_codex}")}"

is_trusted_python_bin() {
  local bin_dir="$1"
  local venv_dir="${bin_dir%/bin}"

  [[ -d "${bin_dir}" && -x "${bin_dir}/python" ]] || return 1
  [[ -f "${venv_dir}/pyvenv.cfg" ]] || return 1
  [[ -O "${venv_dir}" && -O "${bin_dir}" ]] || return 1
  [[ -z "$(find "${venv_dir}" "${bin_dir}" -prune -perm -022 -print -quit)" ]]
}

has_validation_tools() {
  local bin_dir="$1"

  PYTHONDONTWRITEBYTECODE=1 "${bin_dir}/python" - <<'PY' >/dev/null 2>&1
import importlib.util

missing = [
    module
    for module in ("pytest", "ruff", "mypy")
    if importlib.util.find_spec(module) is None
]
raise SystemExit(1 if missing else 0)
PY
}

candidate_python_bins=()
if [[ -n "${CTX_NO_MISTAKES_PYTHON_BIN:-}" ]]; then
  candidate_python_bins+=("${CTX_NO_MISTAKES_PYTHON_BIN}")
fi
candidate_python_bins+=("${pwd_ctx_python_bin}" "${repo_ctx_python_bin}" "${fallback_ctx_python_bin}")

trusted_ctx_python_bin=""
for candidate_python_bin in "${candidate_python_bins[@]}"; do
  if is_trusted_python_bin "${candidate_python_bin}" && has_validation_tools "${candidate_python_bin}"; then
    trusted_ctx_python_bin="${candidate_python_bin}"
    break
  fi
done

if [[ -n "${trusted_ctx_python_bin}" ]]; then
  export PATH="${trusted_ctx_python_bin}:${codex_resources}:${PATH}"
  export CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED="${trusted_ctx_python_bin}"
  if [[ -x "${trusted_ctx_python_bin}/python" ]]; then
    export VIRTUAL_ENV="${VIRTUAL_ENV:-${trusted_ctx_python_bin%/bin}}"
  fi
else
  export PATH="${codex_resources}:${PATH}"
fi
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PIP_DISABLE_PIP_VERSION_CHECK="${PIP_DISABLE_PIP_VERSION_CHECK:-1}"

exec "${real_codex}" "$@"
