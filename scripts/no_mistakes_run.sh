#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"
action="${1:-}"

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

append_agent_wrapper_venv() {
  local config_path="${HOME:-}/.no-mistakes/config.yaml"
  local wrapper_path
  local wrapper_root

  [[ -f "${config_path}" ]] || return 0
  wrapper_path="$(awk '/^[[:space:]]+codex:[[:space:]]/ {print $2; exit}' "${config_path}")"
  [[ -n "${wrapper_path}" && -f "${wrapper_path}" ]] || return 0
  wrapper_root="$(cd -- "$(dirname -- "${wrapper_path}")/.." && pwd -P)"
  candidate_python_bins+=("${wrapper_root}/.venv/bin")
}

candidate_python_bins=()
if [[ -n "${CTX_NO_MISTAKES_PYTHON_BIN:-}" ]]; then
  candidate_python_bins+=("${CTX_NO_MISTAKES_PYTHON_BIN}")
fi
candidate_python_bins+=("${PWD}/.venv/bin" "${repo_root}/.venv/bin")
append_agent_wrapper_venv
candidate_python_bins+=("/tmp/ctx-verify-venv/bin")

trusted_ctx_python_bin=""
for candidate_python_bin in "${candidate_python_bins[@]}"; do
  if is_trusted_python_bin "${candidate_python_bin}" && has_validation_tools "${candidate_python_bin}"; then
    trusted_ctx_python_bin="${candidate_python_bin}"
    break
  fi
done

if [[ -z "${trusted_ctx_python_bin}" ]]; then
  echo "No trusted ctx validation Python found with pytest, ruff, and mypy." >&2
  exit 127
fi

export PATH="${trusted_ctx_python_bin}:${PATH}"
export VIRTUAL_ENV="${trusted_ctx_python_bin%/bin}"
export CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED="${trusted_ctx_python_bin}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PIP_DISABLE_PIP_VERSION_CHECK="${PIP_DISABLE_PIP_VERSION_CHECK:-1}"

run_gate() {
  local intent=""
  local intent_file=""
  local no_mistakes_args=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --intent)
        [[ $# -ge 2 ]] || {
          echo "Missing value for --intent" >&2
          exit 64
        }
        intent="$2"
        shift 2
        ;;
      --intent=*)
        intent="${1#--intent=}"
        shift
        ;;
      --intent-file)
        [[ $# -ge 2 ]] || {
          echo "Missing value for --intent-file" >&2
          exit 64
        }
        intent_file="$2"
        shift 2
        ;;
      --intent-file=*)
        intent_file="${1#--intent-file=}"
        shift
        ;;
      --skip)
        [[ $# -ge 2 ]] || {
          echo "Missing value for --skip" >&2
          exit 64
        }
        no_mistakes_args+=("--skip" "$2")
        shift 2
        ;;
      --skip=*|--yes|-y)
        no_mistakes_args+=("$1")
        shift
        ;;
      *)
        echo "Unsupported gate argument: $1" >&2
        exit 64
        ;;
    esac
  done

  if [[ -n "${intent}" && -n "${intent_file}" ]]; then
    echo "Use either --intent or --intent-file, not both." >&2
    exit 64
  fi
  if [[ -n "${intent_file}" ]]; then
    [[ -f "${intent_file}" ]] || {
      echo "Intent file not found: ${intent_file}" >&2
      exit 64
    }
    intent="$(<"${intent_file}")"
  fi
  if [[ -z "${intent//[[:space:]]/}" ]]; then
    echo "gate requires --intent or --intent-file with a narrow task statement." >&2
    exit 64
  fi
  command -v no-mistakes >/dev/null 2>&1 || {
    echo "no-mistakes is required for gate." >&2
    exit 127
  }

  python scripts/local_fast_gate.py --profile smoke --summary-json .gate/local-fast-smoke.json
  python scripts/local_fast_gate.py --profile pr --summary-json .gate/local-fast.json
  exec no-mistakes axi run --intent "${intent}" "${no_mistakes_args[@]}"
}

case "${action}" in
  fast)
    exec python scripts/local_fast_gate.py --profile pr --summary-json .gate/local-fast.json "${@:2}"
    ;;
  gate)
    run_gate "${@:2}"
    ;;
  test)
    exec python scripts/ci_preflight.py --profile pr
    ;;
  lint)
    python -m ruff check .
    python -m ruff format --check src hooks scripts
    exec python -m mypy src
    ;;
  format)
    exec python -m ruff format src hooks scripts
    ;;
  *)
    echo "Usage: scripts/no_mistakes_run.sh {fast|gate|test|lint|format}" >&2
    exit 64
    ;;
esac
