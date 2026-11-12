#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/start_all.sh [options]

Initialize missing or changed dependencies and start the local QwenPawData stack:
  - Graph store           local Neo4j when GRAPH_BACKEND=neo4j;
                          shared Holo AGE when GRAPH_BACKEND=holo_age
  - Auth API              http://localhost:8088
  - Context API           http://localhost:8765
  - Context frontend      http://localhost:3000  (includes the User Center)
  - Host API              http://localhost:8089  (QwenPawData Host backend)
  - Host frontend         http://localhost:3100  (Data Console UI)

Options:
  --install               Reinstall Python and frontend dependencies
  --skip-install          Do not install missing dependencies
  --skip-host             Do not start QwenPaw Host
  --skip-host-api         Start Host frontend only; skip Host API
  --reload                Enable backend reload mode
  --context-reload        Alias of --reload
  --no-reload             Disable backend reload mode (default)
  -h, --help              Show this help

Environment:
  QWENPAW_DATA_ENV_FILE        Optional dotenv file. Default: repository root .env
  UV                      uv command. Default: uv
  NPM                     npm-compatible command. Default: npm
  NPM_REGISTRY            npm registry. Default: https://registry.npmjs.org
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
context_root="${repo_root}/packages/qwenpaw-data-context"
context_frontend="${context_root}/frontend"
auth_root="${repo_root}/packages/qwenpaw-data-auth"
tracing_root="${repo_root}/packages/qwenpaw-data-tracing"
python_cmd="${context_root}/.venv/bin/python"
requirements_file="${context_root}/requirements.lock.txt"
uv_cmd="${UV:-uv}"
npm_cmd="${NPM:-npm}"
npm_registry="${NPM_REGISTRY:-https://registry.npmjs.org}"
host_frontend_root="${repo_root}/packages/qwenpaw-data-console/frontend"
host_frontend_port="3100"
host_api_python="${repo_root}/.venv/bin/python"
context_port="${CONTEXT_PORT:-8765}"
install_mode="auto"
reload=0
skip_host=0
skip_host_api=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install)
      install_mode="force"
      shift
      ;;
    --skip-install)
      install_mode="skip"
      shift
      ;;
    --skip-host)
      skip_host=1
      shift
      ;;
    --skip-host-api)
      skip_host_api=1
      shift
      ;;
    --reload|--context-reload)
      reload=1
      shift
      ;;
    --no-reload)
      reload=0
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

start_host_frontend=1
start_host_api=1
if [[ "${skip_host}" == "1" ]]; then
  start_host_frontend=0
  start_host_api=0
elif [[ "${skip_host_api}" == "1" ]]; then
  start_host_api=0
fi

# shellcheck source=scripts/common/env.sh
source "${script_dir}/common/env.sh"
ensure_qwenpaw_data_env_file

env_file="$(qwenpaw_data_env_file)"
if [[ ! -f "${env_file}" ]]; then
  touch "${env_file}"
fi

get_env_value() {
  local key="$1"
  local raw
  raw="$(
    awk -v key="${key}" '
      index($0, key "=") == 1 {
        print substr($0, length(key) + 2)
        exit
      }
    ' "${env_file}"
  )"
  # .env may quote values that contain '$'; strip one matching pair.
  case "${raw}" in
    \'*\') raw="${raw:1:${#raw}-2}" ;;
    \"*\") raw="${raw:1:${#raw}-2}" ;;
  esac
  printf '%s\n' "${raw}"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local tmp_file
  tmp_file="$(mktemp "${env_file}.XXXXXX")"
  awk -v key="${key}" -v value="${value}" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 {
      if (!found) {
        print key "=" value
        found = 1
      }
      next
    }
    { print }
    END {
      if (!found) print key "=" value
    }
  ' "${env_file}" > "${tmp_file}"
  mv "${tmp_file}" "${env_file}"
}

set_env_default() {
  local key="$1"
  local value="$2"
  if [[ -z "$(get_env_value "${key}")" ]]; then
    set_env_value "${key}" "${value}"
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
    return
  fi
  echo "Neither openssl nor python3 is available to generate QWENPAW_DATA_AUTH_SECRET." >&2
  exit 1
}

mkdir -p "${repo_root}/data"
set_env_default "QWENPAW_DATA_AUTH_SECRET" "$(generate_secret)"
set_env_default "QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN" "$(generate_secret)"
set_env_default "QWENPAW_DATA_DB_URL" "sqlite+aiosqlite:///${repo_root}/qwenpaw_data.db"
set_env_default "QWENPAW_DATA_SERVICE_SECRET" "$(generate_secret)"
set_env_default "QWENPAW_DATA_AUTH_URL" "http://localhost:8088"
set_env_default "QWENPAW_DATA_ENV" "development"
set_env_default "CORS_ALLOWED_ORIGINS" "http://localhost:3000"
set_env_default "VITE_AUTH_URL" "http://localhost:8088"
set_env_default "VITE_AUTH_FRONTEND_URL" "http://localhost:3000"
set_env_default "VITE_DEFAULT_APP_URL" "http://localhost:3000"
set_env_default "VITE_CONTEXT_FRONTEND_URL" "http://localhost:3000"
set_env_default "VITE_HOST_FRONTEND_URL" "http://localhost:${host_frontend_port}"
set_env_default "QWENPAW_DATA_WORKSPACE_DATA_ROOT" "${repo_root}/data/workspaces"
set_env_default "QWENPAW_DATA_WORKSPACE_SECRET_ROOT" "${repo_root}/data/workspaces/.secrets"

load_qwenpaw_data_env

graph_backend="$(qwenpaw_data_graph_backend)" || exit $?
if [[ "${graph_backend}" == "holo_age" ]]; then
  qwenpaw_data_require_holo_age_env || exit $?
fi

# Local-only bridge for the pre-existing default Workspace. Newly provisioned
# Workspaces must use endpoint_ref/secret_ref and never this fallback.
# Only enable on neo4j: holo_age must not fall back to a local Neo4j graph.
if [[ "${QWENPAW_DATA_ENV:-development}" == "development" &&
      "${graph_backend}" == "neo4j" ]]; then
  export QWENPAW_DATA_ALLOW_LEGACY_DEFAULT_GRAPH=1
else
  export QWENPAW_DATA_ALLOW_LEGACY_DEFAULT_GRAPH="${QWENPAW_DATA_ALLOW_LEGACY_DEFAULT_GRAPH:-0}"
fi
export QWENPAW_DATA_RAW_CYPHER_ENABLED="${QWENPAW_DATA_RAW_CYPHER_ENABLED:-0}"
export QWENPAW_DATA_ALLOW_UNMIGRATED_ENDPOINTS="${QWENPAW_DATA_ALLOW_UNMIGRATED_ENDPOINTS:-0}"

# The Host backend runs natively from the uv workspace venv, not from a
# QwenPaw Docker image. Keep it on loopback by default: it is reached through
# the Host frontend dev proxy, never directly by the browser.
host_api_port="${QWENPAW_PORT:-8089}"
host_api_bind="${QWENPAW_HOST:-127.0.0.1}"

resolve_command() {
  local command_name="$1"
  if command -v "${command_name}" >/dev/null 2>&1; then
    command -v "${command_name}"
    return 0
  fi
  local candidate
  for candidate in "${HOME}/.local/bin/${command_name}" "${HOME}/.cargo/bin/${command_name}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

if ! uv_cmd="$(resolve_command "${uv_cmd}")"; then
  echo "uv was not found. Install it with: brew install uv" >&2
  exit 1
fi
if [[ "${graph_backend}" == "neo4j" ]] && ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI was not found. Install Docker Desktop to start local Neo4j." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for service health checks." >&2
  exit 1
fi

ensure_docker_running() {
  if docker info >/dev/null 2>&1; then
    return
  fi

  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Docker daemon is not running. Start Docker, then retry." >&2
    exit 1
  fi

  if [[ -d "/Applications/Docker.app" ]]; then
    echo "Docker daemon is not running. Opening Docker Desktop..."
    open -a Docker
  elif command -v colima >/dev/null 2>&1; then
    echo "Docker daemon is not running. Starting Colima..."
    run colima start
    local attempt
    for attempt in 1 2 3 4 5; do
      if docker info >/dev/null 2>&1; then
        echo "Docker daemon is ready."
        return
      fi
      sleep 2
    done

    echo "Colima is running but Docker is unreachable. Restarting Colima..."
    run colima stop
    run colima start
  else
    cat >&2 <<'EOF'
Docker CLI is installed, but no Docker daemon is available.
Install and start one of:
  brew install --cask docker
  brew install colima docker && colima start
Then rerun ./scripts/start_all.sh.
EOF
    exit 1
  fi

  local timeout="${DOCKER_START_TIMEOUT:-120}"
  local started_at now
  started_at="$(date +%s)"
  until docker info >/dev/null 2>&1; do
    now="$(date +%s)"
    if (( now - started_at >= timeout )); then
      echo "Timed out waiting for Docker after ${timeout}s." >&2
      exit 1
    fi
    sleep 2
  done
  echo "Docker daemon is ready."
}

if [[ "${graph_backend}" == "neo4j" ]]; then
  ensure_docker_running
else
  echo "Graph backend: holo_age (${HOLO_AGE_HOST}:${HOLO_AGE_PORT:-8099}/${HOLO_AGE_DATABASE})"
fi

install_python_dependencies() {
  local install_requirements=0
  local create_venv=0

  if [[ ! -x "${python_cmd}" ]]; then
    create_venv=1
    install_requirements=1
  elif ! "${python_cmd}" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    install_requirements=1
  fi
  if [[ "${install_mode}" == "force" ]]; then
    create_venv=1
    install_requirements=1
  fi

  if [[ "${create_venv}" == "1" ]]; then
    run "${uv_cmd}" venv --no-project --allow-existing --python 3.12 "${context_root}/.venv"
  fi

  if [[ "${install_requirements}" == "1" ]]; then
    run "${uv_cmd}" pip install --python "${python_cmd}" -r "${requirements_file}"
  fi

  if [[ "${install_mode}" == "force" ]] ||
     ! "${python_cmd}" -c "import qwenpaw_data_auth" >/dev/null 2>&1; then
    run "${uv_cmd}" pip install --python "${python_cmd}" --editable "${auth_root}"
  fi

  if [[ "${install_mode}" == "force" ]] ||
     ! "${python_cmd}" -c "import qwenpaw_data.tracing" >/dev/null 2>&1; then
    # context 以 --no-deps 方式 editable 安装，workspace 依赖需单独装。
    run "${uv_cmd}" pip install --python "${python_cmd}" --editable "${tracing_root}"
  fi

  if [[ "${install_mode}" == "force" ]] ||
     ! "${python_cmd}" -c "import context_manager" >/dev/null 2>&1; then
    run "${uv_cmd}" pip install --python "${python_cmd}" --editable "${context_root}" --no-deps
  fi

}

frontend_dependency_fingerprint() {
  local frontend_root="$1"
  local package_json="${frontend_root}/package.json"
  local package_lock="${frontend_root}/package-lock.json"

  {
    printf 'package.json '
    cksum < "${package_json}"
    if [[ -f "${package_lock}" ]]; then
      printf 'package-lock.json '
      cksum < "${package_lock}"
    fi
  } | cksum | awk '{ print $1 ":" $2 }'
}

install_frontend_dependencies() {
  local frontend_root="$1"
  local vite_bin="${frontend_root}/node_modules/.bin/vite"
  # Keep auto mode aware of manifest changes without reinstalling on every start.
  local dependency_stamp="${frontend_root}/node_modules/.qwenpaw-data-dependency-fingerprint"
  local expected_fingerprint
  local needs_install=0

  expected_fingerprint="$(frontend_dependency_fingerprint "${frontend_root}")"

  if [[ ! -x "${vite_bin}" ]]; then
    needs_install=1
  fi
  if grep -q '"terser"' "${frontend_root}/package.json" &&
     [[ ! -e "${frontend_root}/node_modules/terser/package.json" ]]; then
    needs_install=1
  fi
  if [[ "${install_mode}" == "force" ]]; then
    needs_install=1
  fi
  if [[ ! -f "${dependency_stamp}" ]] ||
     [[ "$(<"${dependency_stamp}")" != "${expected_fingerprint}" ]]; then
    echo "Frontend dependency manifest changed: ${frontend_root}"
    needs_install=1
  fi

  if [[ "${needs_install}" == "1" ]]; then
    if ! npm_cmd="$(resolve_command "${npm_cmd}")"; then
      echo "npm was not found. Install Node.js with: brew install node" >&2
      exit 1
    fi
    run "${npm_cmd}" install --prefix "${frontend_root}" --no-package-lock --registry="${npm_registry}"
    expected_fingerprint="$(frontend_dependency_fingerprint "${frontend_root}")"
    printf '%s\n' "${expected_fingerprint}" > "${dependency_stamp}"
  fi
}

install_host_backend_dependencies() {
  if [[ "${install_mode}" != "force" ]] &&
     "${host_api_python}" -c "import qwenpaw_data.host.core.api.app" >/dev/null 2>&1; then
    return
  fi
  # The root project declares no dependencies, so a plain `uv sync` would not
  # install the workspace members. --all-packages is required here.
  ( cd "${repo_root}" && run "${uv_cmd}" sync --all-packages )
}

if [[ "${install_mode}" != "skip" ]]; then
  install_python_dependencies
  install_frontend_dependencies "${context_frontend}"
  if [[ "${start_host_api}" == "1" ]]; then
    install_host_backend_dependencies
  fi
  if [[ "${start_host_frontend}" == "1" ]]; then
    install_frontend_dependencies "${host_frontend_root}"
  fi
fi

if [[ ! -x "${python_cmd}" ]]; then
  echo "Python environment is missing: ${python_cmd}" >&2
  echo "Rerun without --skip-install." >&2
  exit 1
fi
if ! "${python_cmd}" -c "import qwenpaw_data_auth, context_manager, qwenpaw_data.tracing" >/dev/null 2>&1; then
  echo "Auth, Context or Tracing Python package is not installed." >&2
  echo "Rerun without --skip-install." >&2
  exit 1
fi
if [[ ! -x "${context_frontend}/node_modules/.bin/vite" ]]; then
  echo "Frontend dependencies are missing. Rerun without --skip-install." >&2
  exit 1
fi
if [[ "${start_host_frontend}" == "1" ]] &&
   [[ ! -x "${host_frontend_root}/node_modules/.bin/vite" ]]; then
  echo "Host frontend dependencies are missing. Rerun without --skip-install." >&2
  exit 1
fi
if [[ "${start_host_api}" == "1" ]] &&
   ! "${host_api_python}" -c "import qwenpaw_data.host.core.api.app" >/dev/null 2>&1; then
  echo "qwenpaw-data-host-core is not installed in ${host_api_python}." >&2
  echo "Run 'uv sync --all-packages', or rerun without --skip-install." >&2
  exit 1
fi

check_port_available() {
  local label="$1"
  local port="$2"
  local pids

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof is required to check port ${port}." >&2
    exit 1
  fi

  pids="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi

  echo "${label} port ${port} is already in use by process(es): ${pids//$'\n'/ }. Stop them explicitly or choose another port." >&2
  exit 1
}

check_port_available "Auth API" 8088
if [[ "${start_host_api}" == "1" ]]; then
  check_port_available "Host API" "${host_api_port}"
fi
if [[ "${start_host_frontend}" == "1" ]]; then
  check_port_available "Host frontend" "${host_frontend_port}"
fi

context_pid=""
auth_api_pid=""
provisioner_pid=""
host_api_pid=""
host_frontend_pid=""

cleanup() {
  trap - EXIT INT TERM
  local pid
  for pid in "${provisioner_pid}" "${auth_api_pid}" "${context_pid}" "${host_api_pid}" "${host_frontend_pid}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
      kill -TERM "${pid}" >/dev/null 2>&1 || true
    fi
  done
  for pid in "${provisioner_pid}" "${auth_api_pid}" "${context_pid}" "${host_api_pid}" "${host_frontend_pid}"; do
    if [[ -n "${pid}" ]]; then
      wait "${pid}" >/dev/null 2>&1 || true
    fi
  done
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

context_args=(--context-port "${context_port}")
if [[ "${reload}" == "1" ]]; then
  context_args+=(--context-reload)
fi
"${script_dir}/start_local.sh" "${context_args[@]+"${context_args[@]}"}" &
context_pid="$!"

auth_api_args=(
  -m uvicorn
  qwenpaw_data_auth.server:app
  --host 127.0.0.1
  --port 8088
)
if [[ "${reload}" == "1" ]]; then
  # 只监听 auth 自己的源码；否则默认监听整个仓库根目录，
  # context/host 包的改动（甚至别的 worktree 活动）都会把 auth 重启，
  # 顺带打断登录态刷新与正在进行的请求。
  auth_api_args+=(--reload --reload-dir "packages/qwenpaw-data-auth/src")
fi
"${python_cmd}" "${auth_api_args[@]}" &
auth_api_pid="$!"

if [[ "${start_host_api}" == "1" ]]; then
  host_api_args=(
    -m uvicorn
    qwenpaw_data.host.core.api.app:app
    --host "${host_api_bind}"
    --port "${host_api_port}"
  )
  if [[ "${reload}" == "1" ]]; then
    # 只监听 host-core 与 auth 源码；默认监听整个仓库根目录会让
    # context 包的改动也把 Host API 重启，纯属误伤。
    host_api_args+=(
      --reload
      --reload-dir "packages/qwenpaw-data-host-core/src"
      --reload-dir "packages/qwenpaw-data-auth/src"
    )
  fi
  "${host_api_python}" "${host_api_args[@]}" &
  host_api_pid="$!"
fi

if [[ "${start_host_frontend}" == "1" ]]; then
  (
    cd "${host_frontend_root}"
    QWENPAW_DATA_HOST_API_BASE_URL="http://localhost:${host_api_port}" \
      "${host_frontend_root}/node_modules/.bin/vite" \
        --host "127.0.0.1" \
        --port "${host_frontend_port}" \
        --strictPort
  ) &
  host_frontend_pid="$!"
fi

wait_for_url() {
  local label="$1"
  local url="$2"
  local timeout="${3:-120}"
  local started_at now
  started_at="$(date +%s)"

  while ! curl --fail --silent --output /dev/null "${url}"; do
    if ! kill -0 "${context_pid}" >/dev/null 2>&1 ||
       ! kill -0 "${auth_api_pid}" >/dev/null 2>&1 ||
       { [[ -n "${host_api_pid}" ]] &&
         ! kill -0 "${host_api_pid}" >/dev/null 2>&1; }; then
      echo "A service exited while waiting for ${label}." >&2
      return 1
    fi
    now="$(date +%s)"
    if (( now - started_at >= timeout )); then
      echo "Timed out waiting for ${label}: ${url}" >&2
      return 1
    fi
    sleep 1
  done
  printf '%-20s %s\n' "${label}:" "${url}"
}

echo "Waiting for local services..."
wait_for_url "Auth API" "http://localhost:8088/api/health"

# The provisioner owns Docker and activates Workspaces regardless of the
# control-plane backend: SQLite (local dev) or Hologres/PostgreSQL. It reads
# the queue through qwenpaw_data_auth.database, which normalises QWENPAW_DATA_DB_URL, so
# the only gate here is development mode + a configured control plane.
if [[ "${QWENPAW_DATA_ENV:-development}" == "development" &&
      -n "${QWENPAW_DATA_DB_URL:-}" ]]; then
  "${python_cmd}" "${script_dir}/common/workspace_provisioner.py" \
    --dsn "${QWENPAW_DATA_DB_URL}" &
  provisioner_pid="$!"
  case "${QWENPAW_DATA_DB_URL}" in
    sqlite*:///*) _cp_kind="SQLite control plane" ;;
    *) _cp_kind="Hologres/PostgreSQL control plane" ;;
  esac
  echo "Workspace provisioner: enabled (${_cp_kind}, GRAPH_BACKEND=${graph_backend})"
  unset _cp_kind
else
  echo "Workspace provisioner: disabled (needs QWENPAW_DATA_ENV=development and QWENPAW_DATA_DB_URL)"
fi

wait_for_url "Context API" "http://localhost:${context_port}/api/health"
wait_for_url "Context frontend" "http://localhost:3000"
if [[ "${start_host_api}" == "1" ]]; then
  # Host Core exposes no /api/health route; /api/auth/status is its only
  # unauthenticated endpoint, so it doubles as the readiness probe.
  wait_for_url "Host API" "http://localhost:${host_api_port}/api/auth/status"
fi
if [[ "${start_host_frontend}" == "1" ]]; then
  wait_for_url "Host frontend" "http://localhost:${host_frontend_port}"
fi

cat <<'EOF'

All local services are ready. Press Ctrl+C to stop them.
EOF

while true; do
  if ! kill -0 "${context_pid}" >/dev/null 2>&1; then
    echo "Context stack exited unexpectedly." >&2
    exit 1
  fi
  if ! kill -0 "${auth_api_pid}" >/dev/null 2>&1; then
    echo "Auth API exited unexpectedly." >&2
    exit 1
  fi
  if [[ -n "${provisioner_pid}" ]] &&
     ! kill -0 "${provisioner_pid}" >/dev/null 2>&1; then
    echo "Workspace provisioner exited unexpectedly." >&2
    exit 1
  fi
  if [[ -n "${host_api_pid}" ]] &&
     ! kill -0 "${host_api_pid}" >/dev/null 2>&1; then
    echo "Host API exited unexpectedly." >&2
    exit 1
  fi
  if [[ -n "${host_frontend_pid}" ]] &&
     ! kill -0 "${host_frontend_pid}" >/dev/null 2>&1; then
    echo "Host frontend exited unexpectedly." >&2
    exit 1
  fi
  sleep 1
done
