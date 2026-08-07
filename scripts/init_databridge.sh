#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/init_databridge.sh [options]

Initialize the DataBridge local environment.

This creates or reuses packages/datapaw-context/.venv, installs the locked
DataBridge dependencies, installs datapaw-context itself in editable mode without
re-resolving dependencies, installs the DataBridge frontend dependencies, builds
the frontend, and creates the root .env from .env.example if needed.

Options:
  --skip-build                Do not build the DataBridge frontend
  --skip-npm-install          Reuse existing DataBridge frontend node_modules
  --python PYTHON             Python version or executable for uv venv.
                              Default: packages/datapaw-context/.python-version or 3.12
  --uv CMD                    uv command. Default: ${UV:-uv}, with common user paths fallback
  --npm CMD                   npm-compatible command. Default: ${NPM:-npm}
  -h, --help                  Show this help

Environment:
  DATAPAW_ENV_FILE           Optional dotenv file. Default: repository root .env
  UV                          uv command when --uv is omitted
  NPM                         npm-compatible command when --npm is omitted
  UV_DEFAULT_INDEX            Optional Python package index mirror used by uv
EOF
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
context_root="${repo_root}/packages/datapaw-context"
frontend_root="${context_root}/frontend"
requirements_file="${context_root}/requirements.lock.txt"

# shellcheck source=scripts/env.sh
source "${script_dir}/env.sh"
load_datapaw_env

python_spec="3.12"
if [[ -f "${context_root}/.python-version" ]]; then
  python_spec="$(tr -d '[:space:]' < "${context_root}/.python-version")"
  if [[ -z "${python_spec}" ]]; then
    python_spec="3.12"
  fi
fi
uv_cmd="${UV:-uv}"
npm_cmd="${NPM:-npm}"
skip_build=0
skip_npm_install=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      skip_build=1
      shift
      ;;
    --skip-npm-install)
      skip_npm_install=1
      shift
      ;;
    --python)
      python_spec="${2:?--python requires a value}"
      shift 2
      ;;
    --uv)
      uv_cmd="${2:?--uv requires a value}"
      shift 2
      ;;
    --npm)
      npm_cmd="${2:?--npm requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ensure_datapaw_env_file
ensure_datapaw_neo4j_password
load_datapaw_env

resolve_uv() {
  if command -v "${uv_cmd}" >/dev/null 2>&1; then
    command -v "${uv_cmd}"
    return 0
  fi
  local candidate
  for candidate in "${HOME}/.local/bin/uv" "${HOME}/.cargo/bin/uv"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

if [[ ! -d "${context_root}" ]]; then
  echo "datapaw-context package not found: ${context_root}" >&2
  exit 1
fi
if [[ ! -f "${requirements_file}" ]]; then
  echo "DataBridge requirements lock file not found: ${requirements_file}" >&2
  exit 1
fi
if [[ ! -f "${frontend_root}/package.json" ]]; then
  echo "DataBridge frontend package not found: ${frontend_root}/package.json" >&2
  exit 1
fi

if ! uv_cmd="$(resolve_uv)"; then
  echo "uv was not found. Install uv or pass --uv /path/to/uv." >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js was not found. Install Node.js before initializing the DataBridge frontend." >&2
  exit 1
fi
if ! command -v "${npm_cmd}" >/dev/null 2>&1; then
  echo "npm-compatible command was not found: ${npm_cmd}" >&2
  echo "Install Node.js/npm or pass --npm /path/to/npm." >&2
  exit 1
fi
npm_cmd="$(command -v "${npm_cmd}")"

cd "${context_root}"

run "${uv_cmd}" venv --no-project --allow-existing --python "${python_spec}" "${context_root}/.venv"

python_cmd="${context_root}/.venv/bin/python"
if [[ ! -x "${python_cmd}" ]]; then
  echo "Expected Python environment was not created: ${python_cmd}" >&2
  exit 1
fi

run "${uv_cmd}" pip install --python "${python_cmd}" -r "${requirements_file}"
run "${uv_cmd}" pip install --python "${python_cmd}" --editable "${context_root}" --no-deps

if [[ "${skip_npm_install}" != "1" ]]; then
  if [[ -f "${frontend_root}/package-lock.json" ]]; then
    run "${npm_cmd}" ci --prefix "${frontend_root}"
  else
    run "${npm_cmd}" install --prefix "${frontend_root}"
  fi
fi

vite_bin="${frontend_root}/node_modules/.bin/vite"
if [[ ! -x "${vite_bin}" ]]; then
  echo "DataBridge frontend dependencies are missing: ${vite_bin}" >&2
  echo "Rerun without --skip-npm-install." >&2
  exit 1
fi

if [[ "${skip_build}" != "1" ]]; then
  run "${npm_cmd}" run build --prefix "${frontend_root}"
  if [[ ! -f "${frontend_root}/dist/index.html" ]]; then
    echo "DataBridge frontend build did not create: ${frontend_root}/dist/index.html" >&2
    exit 1
  fi
fi

printf 'DataBridge initialization complete.\n'
