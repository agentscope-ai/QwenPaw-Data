#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/init_local.sh [options]

Initialize the recommended local full-stack environment:
  1. DataBridge runtime
  2. Hash-locked DataPaw workspace dependencies and editable packages
  3. DataBridge MCP client for the DataPaw Host workspace

Options:
  --skip-build                Reuse existing frontend builds
  --skip-npm-install          Reuse existing frontend node_modules
  --context-python PYTHON     Python version/executable for DataBridge init
  --uv CMD                    uv command for local initialization
  --npm CMD                   npm-compatible command for frontend initialization
  --skip-context              Skip DataBridge initialization
  --skip-mcp-config           Do not configure the default DataBridge MCP client
  --skip-cli-link             Do not expose datapaw in the user CLI bin directory
  -h, --help                  Show this help

Environment:
  DATAPAW_ENV_FILE            Optional dotenv file. Default: repository root .env
  DATAPAW_HOME                DataPaw home directory. Default: ~/.datapaw
  DATAPAW_CM_BASE_URL         DataBridge API origin. Default: http://127.0.0.1:8765
  DATAPAW_CLI_BIN_DIR         Directory for the datapaw command link. Default: ~/.local/bin
  UV                          uv command when --uv is omitted
  UV_DEFAULT_INDEX            Optional Python package mirror used without rewriting uv.lock
EOF
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

run_quiet() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@" >/dev/null
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

# shellcheck source=scripts/env.sh
source "${script_dir}/env.sh"
load_datapaw_env

context_args=()
skip_context=0
skip_mcp_config=0
skip_cli_link=0
uv_cmd="${UV:-uv}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      context_args+=(--skip-build)
      shift
      ;;
    --skip-npm-install)
      context_args+=(--skip-npm-install)
      shift
      ;;
    --context-python)
      context_args+=(--python "${2:?--context-python requires a value}")
      shift 2
      ;;
    --uv)
      uv_cmd="${2:?--uv requires a value}"
      shift 2
      ;;
    --npm)
      context_args+=(--npm "${2:?--npm requires a value}")
      shift 2
      ;;
    --skip-context)
      skip_context=1
      shift
      ;;
    --skip-mcp-config)
      skip_mcp_config=1
      shift
      ;;
    --skip-cli-link)
      skip_cli_link=1
      shift
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

if ! uv_cmd="$(resolve_uv)"; then
  echo "uv was not found. Install uv or pass --uv /path/to/uv." >&2
  exit 1
fi

cd "${repo_root}"

if [[ "${skip_context}" != "1" ]]; then
  context_args+=(--uv "${uv_cmd}")
  run "${script_dir}/init_databridge.sh" "${context_args[@]+"${context_args[@]}"}"
fi

workspace_venv="${repo_root}/.venv"
workspace_python="${workspace_venv}/bin/python"
workspace_requirements="$(mktemp "${TMPDIR:-/tmp}/datapaw-workspace-requirements.XXXXXX")"
mcp_import_file=""
cleanup_init_files() {
  rm -f "${workspace_requirements}"
  if [[ -n "${mcp_import_file}" ]]; then
    rm -f "${mcp_import_file}"
  fi
}
trap cleanup_init_files EXIT

# Keep uv.lock as the canonical version and hash source, but install through the
# user's configured index. A direct `uv sync --locked` treats a mirror URL as a
# different lock source and rejects an otherwise unchanged lockfile.
run_quiet "${uv_cmd}" export \
  --all-packages \
  --frozen \
  --format requirements-txt \
  --no-emit-workspace \
  --output-file "${workspace_requirements}"
run "${uv_cmd}" venv --allow-existing "${workspace_venv}"
run "${uv_cmd}" pip sync \
  --python "${workspace_python}" \
  --require-hashes \
  "${workspace_requirements}"
run "${uv_cmd}" pip install \
  --python "${workspace_python}" \
  --no-deps \
  --editable "${repo_root}/packages/datapaw-context" \
  --editable "${repo_root}/packages/datapaw-host-core" \
  --editable "${repo_root}/packages/datapaw-cli" \
  --editable "${repo_root}/packages/datapaw-skills"

workspace_datapaw="${repo_root}/.venv/bin/datapaw"
if [[ ! -x "${workspace_datapaw}" ]]; then
  echo "Expected DataPaw CLI was not installed: ${workspace_datapaw}" >&2
  exit 1
fi

cli_bin_dir=""
cli_link=""
install_cli_link() {
  cli_bin_dir="$(datapaw_abs_path "${DATAPAW_CLI_BIN_DIR:-~/.local/bin}")"
  cli_link="${cli_bin_dir}/datapaw"

  run mkdir -p "${cli_bin_dir}"
  if [[ -L "${cli_link}" ]]; then
    local existing_target
    existing_target="$(readlink "${cli_link}")"
    if [[ "${existing_target}" == "${workspace_datapaw}" ]]; then
      printf 'DataPaw CLI command already linked: %s\n' "${cli_link}"
      return
    fi
    if [[ "${existing_target}" == "${repo_root}"/* ]]; then
      # 指向本仓库内旧安装位置（如已废弃的子包 .venv），视为残留并更新。
      echo "Updating stale DataPaw CLI link: ${cli_link} -> ${workspace_datapaw}"
      run ln -sf "${workspace_datapaw}" "${cli_link}"
      return
    fi
    echo "Refusing to replace existing DataPaw CLI link: ${cli_link} -> ${existing_target}" >&2
    echo "Remove it explicitly or rerun with --skip-cli-link." >&2
    exit 1
  fi
  if [[ -e "${cli_link}" ]]; then
    echo "Refusing to replace existing DataPaw CLI command: ${cli_link}" >&2
    echo "Remove it explicitly or rerun with --skip-cli-link." >&2
    exit 1
  fi

  run ln -s "${workspace_datapaw}" "${cli_link}"
}

if [[ "${skip_cli_link}" != "1" ]]; then
  install_cli_link
fi

if [[ "${skip_mcp_config}" != "1" ]]; then
  cm_base_url="${DATAPAW_CM_BASE_URL:-http://127.0.0.1:8765}"
  while [[ "${cm_base_url}" == */ ]]; do
    cm_base_url="${cm_base_url%/}"
  done
  if [[ -z "${cm_base_url}" ]]; then
    echo "DATAPAW_CM_BASE_URL must not be empty." >&2
    exit 1
  fi

  mcp_import_file="$(mktemp "${TMPDIR:-/tmp}/datapaw-databridge-mcp.XXXXXX")"
  (
    umask 077
    cat > "${mcp_import_file}" <<EOF
[
  {
    "name": "databridge",
    "is_stateful": false,
    "mcp_config": {
      "type": "http_mcp",
      "url": "${cm_base_url}/mcp/v1/cm",
      "headers": {},
      "timeout": 2400.0
    },
    "enable_tools": null,
    "disable_tools": null,
    "execution_timeout": 2400.0
  }
]
EOF
  )
  run "${workspace_datapaw}" mcp import "${mcp_import_file}"
fi

printf 'Local initialization complete.\n'
if [[ -n "${cli_link}" ]]; then
  printf 'DataPaw CLI command: %s\n' "${cli_link}"
  case ":${PATH}:" in
    *":${cli_bin_dir}:"*) ;;
    *)
      printf 'Add %s to PATH, then open a new shell.\n' "${cli_bin_dir}"
      ;;
  esac
fi
printf 'Run: datapaw --help\n'
