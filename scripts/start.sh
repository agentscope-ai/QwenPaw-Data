#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/start.sh [--service all|cm|host]

No option: start all services.
  --service cm     DataBridge API/frontend, Auth and Workspace provisioner
  --service host   Host API/frontend only; CM/Auth must be available for chat
  -h, --help       Show this help

Configuration comes from .env or QWENPAW_DATA_ENV_FILE. This only starts services;
use scripts/setup_chat.sh to initialize accounts, models and data.
EOF
}

service=all
if [[ $# -gt 0 ]]; then
  case "$1" in
    -h|--help) [[ $# == 1 ]] || { usage >&2; exit 2; }; usage; exit 0 ;;
    --service)
      [[ $# == 2 ]] || { usage >&2; exit 2; }
      case "$2" in all|cm|host) service="$2" ;; *) usage >&2; exit 2 ;; esac
      ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
fi
selected() { [[ "${service}" == all || "${service}" == "$1" ]]; }

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
host_frontend_root="${repo_root}/packages/qwenpaw-data-console/frontend"
host_frontend_port="3100"
host_api_python="${repo_root}/.venv/bin/python"
context_port="${CONTEXT_PORT:-8765}"
# shellcheck source=scripts/common/env.sh
source "${script_dir}/common/env.sh"
ensure_qwenpaw_data_env_file
load_qwenpaw_data_env
uv_cmd="${UV:-uv}"
npm_cmd="${NPM:-npm}"
npm_registry="${NPM_REGISTRY:-https://registry.npmjs.org}"
auth_port="${AUTH_PORT:-8088}"
context_port="${CONTEXT_PORT:-8765}"
context_frontend_port="${CONTEXT_FRONTEND_PORT:-3000}"
host_frontend_port="${HOST_FRONTEND_PORT:-3100}"
python_cmd="${QWENPAW_DATA_PYTHON:-${python_cmd}}"
host_api_python="${QWENPAW_DATA_PYTHON:-${host_api_python}}"
export CONTEXT_PYTHON="${python_cmd}"

env_file="$(qwenpaw_data_env_file)"
if [[ ! -f "${env_file}" ]]; then
  touch "${env_file}"
fi

setup_on_start="${QWENPAW_DATA_SETUP_ON_START:-0}"
if [[ "${setup_on_start}" == 1 && "${service}" != all ]]; then
  echo "Chat setup requires all services; do not combine it with --service." >&2
  exit 2
fi
startup_timeout=300
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for service readiness checks." >&2
  exit 1
fi

ready_labels=("Auth API" "CM API" "Host API" "DataBridge 前端" "Host 前端")
auth_origin="${QWENPAW_DATA_AUTH_URL:-http://localhost:${auth_port}}"
context_origin="${QWENPAW_DATA_CM_BASE_URL:-http://localhost:${context_port}}"
host_origin="${QWENPAW_DATA_HOST_BASE_URL:-http://localhost:${QWENPAW_PORT:-8089}}"
ready_urls=(
  "${auth_origin%/}/api/health"
  "${context_origin%/}/api/health"
  "${host_origin%/}/api/auth/status"
  "${VITE_CONTEXT_FRONTEND_URL:-http://localhost:${context_frontend_port}}"
  "${VITE_HOST_FRONTEND_URL:-http://localhost:${host_frontend_port}}"
)

run_chat_setup() {
  # Startup may have generated secrets and installed the selected interpreter.
  load_qwenpaw_data_env
  local setup_python="${QWENPAW_DATA_PYTHON:-${context_root}/.venv/bin/python}"
  if [[ ! -x "${setup_python}" ]]; then
    echo "初始化所需 Python 环境不存在：${setup_python}" >&2
    return 1
  fi
  echo "[2/2] 服务已就绪，开始检查资源并初始化聊天配置。"
  # Run in a fresh shell so configuration failures cannot be masked by the
  # launcher's conditional reuse path. This entry remains services-only otherwise.
  bash -c 'source "$1"; chat_setup_configure "$2"' _ \
    "${script_dir}/setup_chat.sh" "${setup_python}"
}

reuse_services() {
  echo "[1/2] 检测到已有或正在启动的服务，等待全部就绪（最多 ${startup_timeout} 秒），不会重复启动。"
  local started_at="$SECONDS" index lock
  local missing=()
  while true; do
    missing=()
    for index in "${!ready_urls[@]}"; do
      if ! curl --fail --silent --connect-timeout 1 --max-time 2 --output /dev/null "${ready_urls[$index]}"; then
        missing+=("${ready_labels[$index]}")
      fi
    done
    # A listening API does not imply the launcher has finished its manifest and
    # provisioner setup. Wait for its explicit handoff before initializing.
    for lock in "${env_file}.services.lock" "${env_file}.cm.services.lock" "${env_file}.host.services.lock"; do
      if [[ -d "${lock}" && ! -f "${lock}/ready" ]]; then
        missing+=("启动器完成运行检查")
      fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
      run_chat_setup || return $?
      echo "聊天配置完成；复用的服务继续由原启动终端管理。"
      return
    fi
    if (( SECONDS - started_at >= startup_timeout )); then
      echo "等待服务超时，尚未就绪：${missing[*]}。请查看原启动终端；尚未执行聊天初始化。" >&2
      return 1
    fi
    echo "正在等待：${missing[*]}……"
    sleep 2
  done
}

# A short-lived shared lock serializes dotenv/dependency preparation. Persistent
# ownership locks prevent overlapping full/single launches, while CM and Host can
# run in different terminals. Their process and manifest ownership stay separate.
startup_lock="${env_file}.services.lock"
manifest_path="${env_file}.services.json"
if [[ "${service}" != all ]]; then
  startup_lock="${env_file}.${service}.services.lock"
  manifest_path="${env_file}.${service}.services.json"
fi
prepare_lock="${env_file}.startup.services.lock"
prepare_owned=0
startup_owned=0
prune_stale_lock() {
  local lock="$1" owner
  if [[ -f "${lock}/pid" ]]; then
    owner=$(cat "${lock}/pid")
    if [[ "${owner}" =~ ^[0-9]+$ ]] && ! kill -0 "${owner}" 2>/dev/null; then
      rm -f "${lock}/pid" "${lock}/ready" "${lock%.lock}.json"
      rmdir "${lock}" 2>/dev/null || true
    fi
  fi
}
release_prepare_lock() {
  if [[ "${prepare_owned}" == 1 ]]; then
    rm -f "${prepare_lock}/pid"
    rmdir "${prepare_lock}" 2>/dev/null || true
    prepare_owned=0
  fi
}
release_startup_lock() {
  release_prepare_lock
  if [[ "${startup_owned}" == 1 ]]; then
    rm -f "${startup_lock}/pid" "${startup_lock}/ready" "${manifest_path}"
    rmdir "${startup_lock}" 2>/dev/null || true
  fi
}
trap release_startup_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for lock in "${env_file}.services.lock" "${env_file}.cm.services.lock" "${env_file}.host.services.lock"; do
  prune_stale_lock "${lock}"
done
if [[ "${setup_on_start}" == 1 ]]; then
  existing=0
  for lock in "${env_file}.services.lock" "${env_file}.cm.services.lock" "${env_file}.host.services.lock"; do
    [[ ! -d "${lock}" ]] || existing=1
  done
  for url in "${ready_urls[@]}"; do
    if curl --fail --silent --connect-timeout 1 --max-time 2 --output /dev/null "${url}"; then
      existing=1
      break
    fi
  done
  if [[ "${existing}" == 1 ]]; then reuse_services; exit 0; fi
fi
prepare_started=$SECONDS
while ! mkdir "${prepare_lock}" 2>/dev/null; do
  prune_stale_lock "${prepare_lock}"
  (( SECONDS - prepare_started < startup_timeout )) || { echo 'Timed out waiting for environment preparation.' >&2; exit 1; }
  sleep 1
done
prepare_owned=1
printf '%s\n' "$$" >"${prepare_lock}/pid"
# Recheck ownership after acquiring the preparation lock to close startup races.
conflict=0
for lock in "${env_file}.services.lock" "${startup_lock}"; do
  prune_stale_lock "${lock}"
  [[ ! -d "${lock}" ]] || conflict=1
done
if [[ "${service}" == all ]]; then
  for lock in "${env_file}.cm.services.lock" "${env_file}.host.services.lock"; do
    prune_stale_lock "${lock}"
    [[ ! -d "${lock}" ]] || conflict=1
  done
fi
if [[ "${conflict}" == 1 ]]; then
  release_prepare_lock
  if [[ "${setup_on_start}" == 1 ]]; then reuse_services; exit 0; fi
  echo "Requested service overlaps an existing launcher for this env; stop its owning terminal first." >&2
  exit 1
fi
mkdir "${startup_lock}"
startup_owned=1
printf '%s\n' "$$" >"${startup_lock}/pid"
# A stopped full-stack record must not shadow fresh per-service records.
if [[ "${service}" != all ]]; then
  rm -f "${env_file}.services.json" "${manifest_path}"
else
  rm -f "${env_file}.cm.services.json" "${env_file}.host.services.json"
fi
load_qwenpaw_data_env
echo "准备依赖并启动 ${service} 服务。首次运行可能需要下载镜像和安装依赖，请等待就绪提示。"

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
set_env_default "QWENPAW_DATA_AUTH_URL" "http://localhost:${auth_port}"
set_env_default "QWENPAW_DATA_CM_BASE_URL" "http://localhost:${context_port}"
set_env_default "QWENPAW_DATA_HOST_BASE_URL" "http://localhost:${QWENPAW_PORT:-8089}"
set_env_default "QWENPAW_DATA_ENV" "development"
set_env_default "CORS_ALLOWED_ORIGINS" "http://localhost:${context_frontend_port},http://localhost:${host_frontend_port}"
set_env_default "VITE_AUTH_URL" "http://localhost:${auth_port}"
set_env_default "VITE_AUTH_FRONTEND_URL" "http://localhost:${context_frontend_port}"
set_env_default "VITE_DEFAULT_APP_URL" "http://localhost:${context_frontend_port}"
set_env_default "VITE_CONTEXT_FRONTEND_URL" "http://localhost:${context_frontend_port}"
set_env_default "VITE_HOST_FRONTEND_URL" "http://localhost:${host_frontend_port}"
set_env_default "QWENPAW_DATA_WORKSPACE_DATA_ROOT" "${repo_root}/data/workspaces"
set_env_default "QWENPAW_DATA_WORKSPACE_SECRET_ROOT" "${repo_root}/data/workspaces/.secrets"

load_qwenpaw_data_env

graph_backend="$(qwenpaw_data_graph_backend)" || exit $?

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

run_uv() {
  if ! uv_cmd="$(resolve_command "${uv_cmd}")"; then
    echo "uv was not found. Install it with: brew install uv" >&2
    exit 1
  fi
  run "${uv_cmd}" --directory "${repo_root}" "$@"
}

install_python_dependencies() {
  if [[ ! -x "${python_cmd}" ]]; then
    run_uv venv --no-project --allow-existing --python 3.12 "${context_root}/.venv"
  fi

  # Install the lock and the local workspace packages against the same versions.
  run_uv pip install --python "${python_cmd}" -r "${requirements_file}" \
    --editable "${auth_root}" --editable "${tracing_root}"
  run_uv pip install --python "${python_cmd}" --editable "${context_root}" --no-deps

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
  local registry_args=()

  expected_fingerprint="$(frontend_dependency_fingerprint "${frontend_root}")"

  if [[ ! -x "${vite_bin}" ]]; then
    needs_install=1
  fi
  if [[ ! -f "${dependency_stamp}" ]] ||
     [[ "$(<"${dependency_stamp}")" != "${expected_fingerprint}" ]]; then
    echo "Frontend dependency manifest changed: ${frontend_root}"
    needs_install=1
  fi

  if [[ "${needs_install}" == "0" && -f "${frontend_root}/package-lock.json" ]] &&
     ! node "${script_dir}/common/check_frontend_dependencies.mjs" "${frontend_root}"; then
    needs_install=1
  fi

  if [[ "${needs_install}" == "1" ]]; then
    if ! npm_cmd="$(resolve_command "${npm_cmd}")"; then
      echo "npm was not found. Install Node.js with: brew install node" >&2
      exit 1
    fi
    if [[ -f "${frontend_root}/package-lock.json" ]]; then
      # Public mirror URLs in the Host lockfile must follow the configured
      # registry too; retain private registry URLs and package integrity hashes.
      if grep -q '"resolved": "https://registry.npmmirror.com/' "${frontend_root}/package-lock.json"; then
        registry_args+=(--replace-registry-host=registry.npmmirror.com)
      fi
      run "${npm_cmd}" ci --prefix "${frontend_root}" --include=dev --no-audit --no-fund \
        --registry="${npm_registry}" ${registry_args[@]+"${registry_args[@]}"}
      run node "${script_dir}/common/check_frontend_dependencies.mjs" "${frontend_root}"
    else
      run "${npm_cmd}" install --prefix "${frontend_root}" --include=dev --no-package-lock --no-audit --no-fund --registry="${npm_registry}"
    fi
    expected_fingerprint="$(frontend_dependency_fingerprint "${frontend_root}")"
    printf '%s\n' "${expected_fingerprint}" > "${dependency_stamp}"
  fi
}

install_host_backend_dependencies() {
  (
    # Use the lock's sources and the same environment that runs Host.
    unset UV_CONFIG_FILE UV_NO_CONFIG UV_INDEX UV_DEFAULT_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_FIND_LINKS UV_NO_INDEX
    export UV_PROJECT_ENVIRONMENT="${repo_root}/.venv"
    run_uv sync --all-packages --locked --inexact
  )
}

if selected cm; then
  if [[ -z "${QWENPAW_DATA_PYTHON:-}" ]]; then install_python_dependencies; fi
  install_frontend_dependencies "${context_frontend}"
  if [[ -n "${QWENPAW_DATA_PYTHON:-}" ]]; then
    "${python_cmd}" -c "import qwenpaw_data_auth, context_manager, qwenpaw_data.tracing" || {
      echo "Install CM dependencies in the selected interpreter before starting." >&2; exit 1;
    }
  fi
  qwenpaw_data_require_free_port "Auth API" "${auth_port}"
fi
if selected host; then
  if [[ -z "${QWENPAW_DATA_PYTHON:-}" ]]; then install_host_backend_dependencies; fi
  if ! selected cm; then install_frontend_dependencies "${context_frontend}"; fi
  install_frontend_dependencies "${host_frontend_root}"
  if [[ -n "${QWENPAW_DATA_PYTHON:-}" ]]; then
    "${host_api_python}" -c "import qwenpaw_data.host.core.api.app" || {
      echo "Install Host dependencies in the selected interpreter before starting." >&2; exit 1;
    }
  fi
  qwenpaw_data_require_free_port "Host API" "${host_api_port}"
  qwenpaw_data_require_free_port "Host frontend" "${host_frontend_port}"
  # The launcher starts Vite directly, so build the CM library before starting Host.
  if ! npm_cmd="$(resolve_command "${npm_cmd}")"; then
    echo "npm was not found. Install Node.js with: brew install node" >&2
    exit 1
  fi
  run "${npm_cmd}" --prefix "${context_frontend}" run build:embedded
fi
release_prepare_lock

context_pid=""
auth_api_pid=""
provisioner_pid=""
host_api_pid=""
host_frontend_pid=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "${status}" == "130" || "${status}" == "143" ]]; then
    echo "收到停止信号，正在停止本次应用服务；数据库容器和数据保留。"
  elif [[ "${status}" != "0" ]]; then
    echo "启动或初始化未完成（退出码 ${status}）。正在停止本次启动的应用服务；已保存的配置及数据库容器、数据保留。" >&2
    if [[ "${setup_on_start}" == 1 ]]; then
      echo "修复上方错误后，重新运行：bash ${script_dir}/setup_chat.sh" >&2
    else
      echo "修复上方错误后，重新运行：bash ${script_dir}/start.sh --service ${service}" >&2
    fi
  fi
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
  release_startup_lock
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if selected cm; then
  auth_api_args=(
    -m uvicorn
    qwenpaw_data_auth.server:app
    --host 127.0.0.1
    --port "${auth_port}"
  )
  "${python_cmd}" "${auth_api_args[@]}" &
  auth_api_pid="$!"
fi

if selected host; then
  host_api_args=(
    -m uvicorn
    qwenpaw_data.host.core.api.app:app
    --host "${host_api_bind}"
    --port "${host_api_port}"
  )
  "${host_api_python}" "${host_api_args[@]}" &
  host_api_pid="$!"

  (
    cd "${host_frontend_root}"
    exec env QWENPAW_DATA_DIRECT_SERVICES=1 QWENPAW_DATA_HOST_API_BASE_URL="http://localhost:${host_api_port}" \
      QWENPAW_DATA_AUTH_URL="${auth_origin}" \
      SERVICE_BASE_URL="${QWENPAW_DATA_CM_BASE_URL:-http://localhost:${context_port}}" \
      "${host_frontend_root}/node_modules/.bin/vite" \
        --host "127.0.0.1" \
        --port "${host_frontend_port}" \
        --strictPort
  ) &
  host_frontend_pid="$!"
fi

services_alive() {
  local pid
  for pid in "${context_pid}" "${auth_api_pid}" "${provisioner_pid}" "${host_api_pid}" "${host_frontend_pid}"; do
    if [[ -n "${pid}" ]] && ! kill -0 "${pid}" 2>/dev/null; then
      echo "A launched service exited unexpectedly (PID ${pid})." >&2
      return 1
    fi
  done
}

wait_for_url() {
  local label="$1"
  local url="$2"
  local timeout="${3:-${startup_timeout}}"
  local started_at now
  started_at="$(date +%s)"

  while ! curl --fail --silent --connect-timeout 1 --max-time 2 --output /dev/null "${url}"; do
    if ! services_alive; then
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
if selected cm; then
  wait_for_url "Auth API" "http://localhost:${auth_port}/api/health"
  # Context validates Host-owned tables on a fresh shared control database.
  # Start Host first; a CM-only launch requires an already initialized Host DB.
  if selected host; then
    wait_for_url "Host schema" "http://localhost:${host_api_port}/api/auth/status"
  fi
  bash "${script_dir}/start_context.sh" --port "${context_port}" &
  context_pid="$!"

  # The provisioner owns Docker and activates Workspaces regardless of the
  # control-plane backend: SQLite (local dev) or PostgreSQL. It reads
  # the queue through qwenpaw_data_auth.database, which normalises QWENPAW_DATA_DB_URL, so
  # the only gate here is development mode + a configured control plane.
  if [[ "${QWENPAW_DATA_ENV:-development}" == "development" &&
        -n "${QWENPAW_DATA_DB_URL:-}" ]]; then
    "${python_cmd}" "${script_dir}/common/workspace_provisioner.py" &
    provisioner_pid="$!"
    case "${QWENPAW_DATA_DB_URL}" in
      sqlite*:///*) _cp_kind="SQLite control plane" ;;
      *) _cp_kind="PostgreSQL control plane" ;;
    esac
    echo "Workspace provisioner: enabled (${_cp_kind}, GRAPH_BACKEND=${graph_backend})"
    unset _cp_kind
  else
    echo "Workspace provisioner: disabled (needs QWENPAW_DATA_ENV=development and QWENPAW_DATA_DB_URL)"
  fi

  wait_for_url "Context API" "http://localhost:${context_port}/api/health"
  wait_for_url "Context frontend" "http://localhost:${context_frontend_port}"
fi
if selected host; then
  # Host Core exposes no /api/health route; /api/auth/status is its only
  # unauthenticated endpoint, so it doubles as the readiness probe.
  wait_for_url "Host API" "http://localhost:${host_api_port}/api/auth/status"
  wait_for_url "Host frontend" "http://localhost:${host_frontend_port}"
fi

manifest_args=(write --service "${service}")
manifest_python="${host_api_python}"
if selected cm; then
  manifest_python="${python_cmd}"
  manifest_args+=(--pid "${auth_api_pid}" --pid "${context_pid}" --auth-url "http://localhost:${auth_port}" --context-url "http://localhost:${context_port}")
  if [[ -n "${provisioner_pid}" ]]; then manifest_args+=(--pid "${provisioner_pid}"); fi
fi
if selected host; then
  manifest_args+=(--pid "${host_api_pid}" --pid "${host_frontend_pid}" --host-url "http://localhost:${host_api_port}")
fi
"${manifest_python}" "${script_dir}/common/service_manifest.py" "${manifest_args[@]}"

touch "${startup_lock}/ready"

if [[ "${service}" == all ]]; then
  echo "All local services are ready / 全部服务已就绪。"
else
  echo "${service} services are ready / 所选服务已就绪（其他服务未启动）。"
fi
if [[ "${setup_on_start}" == "1" ]]; then
  run_chat_setup
  echo "聊天环境准备完成。本终端继续托管服务；按 Ctrl+C 停止本次启动的应用服务。"
else
  echo "服务启动完成（尚未初始化聊天配置）。需要初始化时运行：bash ${script_dir}/setup_chat.sh"
  echo "本终端继续托管服务；按 Ctrl+C 停止应用服务。"
fi
if selected cm; then echo "DataBridge: http://localhost:${context_frontend_port}"; fi
if selected host; then echo "Chat: http://localhost:${host_frontend_port}"; fi

while true; do
  services_alive || exit 1
  sleep 1
done
