#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/start_databridge.sh [options]

Start the local Neo4j service, DataBridge API, and DataBridge frontend.
Run scripts/init_databridge.sh first on a fresh checkout.

Options:
  --host HOST                 Bind host for DataBridge service. Default: 127.0.0.1
  --port PORT                 Bind port for DataBridge service. Default: 8765
  --log-level LEVEL           Log level for uvicorn. Default: info
  --reload                    Enable DataBridge service reload mode
  --skip-frontend             Start only the database and DataBridge API
  -h, --help                  Show this help

Environment:
  QWENPAW_DATA_ENV_FILE           Optional dotenv file. Default: repository root .env
  CONTEXT_HOST                Bind host when --host is omitted
  CONTEXT_PORT                Bind port when --port is omitted
  CONTEXT_LOG_LEVEL           Log level when --log-level is omitted
  FRONTEND_HOST               Bind host for the DataBridge frontend. Default: 127.0.0.1
  DOCKER_START_TIMEOUT        Seconds to wait after opening Docker Desktop on macOS. Default: 120
  CONTEXT_PYTHON              Python executable. Default: packages/qwenpaw-data-context/.venv/bin/python

By default all services bind to loopback only. Set --host/CONTEXT_HOST and
FRONTEND_HOST to 0.0.0.0 explicitly to expose them on the network, and make
sure QWENPAW_DATA_API_TOKEN or QWENPAW_DATA_API_KEYS is configured first.
The DataBridge frontend uses port 3000 with Vite strictPort enabled.
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
frontend_root="${context_root}/frontend"
frontend_port="3000"

# shellcheck source=scripts/env.sh
source "${script_dir}/env.sh"
load_qwenpaw_data_env

host="${CONTEXT_HOST:-127.0.0.1}"
frontend_host="${FRONTEND_HOST:-127.0.0.1}"
port="${CONTEXT_PORT:-8765}"
log_level="${CONTEXT_LOG_LEVEL:-info}"
reload=0
skip_frontend=0
api_pid=""
frontend_pid=""

resolve_command() {
  local candidate="$1"
  if [[ -x "${candidate}" ]]; then
    printf '%s\n' "${candidate}"
    return 0
  fi
  if command -v "${candidate}" >/dev/null 2>&1; then
    command -v "${candidate}"
    return 0
  fi
  return 1
}

if [[ -n "${CONTEXT_PYTHON:-}" ]]; then
  if ! python_cmd="$(resolve_command "${CONTEXT_PYTHON}")"; then
    echo "DataBridge Python executable not found: ${CONTEXT_PYTHON}" >&2
    exit 1
  fi
else
  python_cmd="${context_root}/.venv/bin/python"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      host="${2:?--host requires a value}"
      shift 2
      ;;
    --port)
      port="${2:?--port requires a value}"
      shift 2
      ;;
    --log-level)
      log_level="${2:?--log-level requires a value}"
      shift 2
      ;;
    --reload)
      reload=1
      shift
      ;;
    --skip-frontend)
      skip_frontend=1
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

if [[ ! -d "${context_root}" ]]; then
  echo "qwenpaw-data-context package not found: ${context_root}" >&2
  exit 1
fi
if [[ ! -x "${python_cmd}" ]]; then
  echo "DataBridge Python environment not found: ${python_cmd}" >&2
  echo "Run scripts/init_databridge.sh first, or set CONTEXT_PYTHON to a prepared interpreter." >&2
  exit 1
fi
if [[ "${skip_frontend}" != "1" ]]; then
  if ! command -v node >/dev/null 2>&1; then
    echo "Node.js was not found. Install Node.js or use --skip-frontend for API-only mode." >&2
    exit 1
  fi
  vite_bin="${frontend_root}/node_modules/.bin/vite"
  if [[ ! -x "${vite_bin}" ]]; then
    echo "DataBridge frontend dependencies are missing: ${vite_bin}" >&2
    echo "Run scripts/init_databridge.sh first, or use --skip-frontend for API-only mode." >&2
    exit 1
  fi
fi

url_host() {
  local value="$1"
  if [[ "${value}" == "0.0.0.0" || "${value}" == "::" ]]; then
    value="127.0.0.1"
  fi
  if [[ "${value}" == *:* && "${value}" != \[*\] ]]; then
    printf '[%s]\n' "${value}"
  else
    printf '%s\n' "${value}"
  fi
}

is_loopback_bind_host() {
  local value="${1#[}"
  value="${value%]}"
  case "${value}" in
    localhost|::1|127.0.0.1) return 0 ;;
    *) return 1 ;;
  esac
}

cleanup() {
  trap - EXIT INT TERM

  if [[ -n "${frontend_pid}" ]] && kill -0 "${frontend_pid}" >/dev/null 2>&1; then
    kill -TERM "${frontend_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill -TERM "${api_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${frontend_pid}" ]]; then
    wait "${frontend_pid}" >/dev/null 2>&1 || true
    frontend_pid=""
  fi
  if [[ -n "${api_pid}" ]]; then
    wait "${api_pid}" >/dev/null 2>&1 || true
    api_pid=""
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ensure_docker_running() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker CLI was not found. Install Docker Desktop to start local Neo4j." >&2
    exit 1
  fi

  if docker info >/dev/null 2>&1; then
    return
  fi

  if [[ "$(uname -s)" == "Darwin" ]]; then
    local timeout="${DOCKER_START_TIMEOUT:-120}"
    if [[ ! "${timeout}" =~ ^[0-9]+$ || "${timeout}" -le 0 ]]; then
      echo "DOCKER_START_TIMEOUT must be a positive integer: ${timeout}" >&2
      exit 2
    fi

    echo "Docker daemon is not running. Opening Docker Desktop..."
    if ! open -a Docker >/dev/null 2>&1; then
      echo "Failed to open Docker Desktop. Start Docker manually, then retry." >&2
      exit 1
    fi

    local start_time now elapsed
    start_time="$(date +%s)"
    while true; do
      if docker info >/dev/null 2>&1; then
        echo "Docker daemon is running."
        return
      fi

      now="$(date +%s)"
      elapsed=$((now - start_time))
      if [[ "${elapsed}" -ge "${timeout}" ]]; then
        echo "Timed out waiting for Docker Desktop to start after ${timeout}s." >&2
        echo "Start Docker manually, then retry." >&2
        exit 1
      fi
      sleep 2
    done
  fi

  echo "Docker daemon is not running. Start Docker to start local Neo4j." >&2
  exit 1
}

neo4j_reachable() {
  local bolt_port="${NEO4J_BOLT_PORT:-7687}"
  (exec 3<>"/dev/tcp/127.0.0.1/${bolt_port}") 2>/dev/null
}

start_databases() {
  local env_args=()
  if [[ -f "${QWENPAW_DATA_ENV_FILE:-}" ]]; then
    env_args=(--env-file "${QWENPAW_DATA_ENV_FILE}")
  fi
  if neo4j_reachable; then
    echo "Reusing already-running Neo4j at 127.0.0.1:${NEO4J_BOLT_PORT:-7687} (skipping Docker Compose)."
    return 0
  fi
  ensure_docker_running
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    run docker compose "${env_args[@]}" -f "${context_root}/docker-compose.yml" up -d
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    run docker-compose "${env_args[@]}" -f "${context_root}/docker-compose.yml" up -d
    return
  fi
  echo "Docker Compose was not found. Install Docker Compose to start local Neo4j." >&2
  exit 1
}

cd "${context_root}"

start_databases

cmd=(
  "${python_cmd}" scripts/serve.py
  --host "${host}"
  --port "${port}"
  --log-level "${log_level}"
)
if [[ "${reload}" == "1" ]]; then
  cmd+=(--reload)
fi
if [[ "${skip_frontend}" != "1" ]] && ! is_loopback_bind_host "${frontend_host}"; then
  # An externally reachable Vite server proxies /api to the loopback backend.
  # Require backend authentication even though Uvicorn itself is not exposed.
  cmd+=(--require-auth)
fi

printf '+'
printf ' %q' "${cmd[@]}"
printf ' &\n'
"${cmd[@]}" &
api_pid="$!"

if [[ "${skip_frontend}" != "1" ]]; then
  context_url="http://$(url_host "${host}"):${port}"
  printf '+ VITE_API_BASE_URL=%q SERVICE_BASE_URL=%q' "" "${context_url}"
  printf ' %q' \
    "${vite_bin}" \
    "${frontend_root}" \
    --host "${frontend_host}" \
    --port "${frontend_port}" \
    --strictPort
  printf ' &\n'
  VITE_API_BASE_URL="" SERVICE_BASE_URL="${context_url}" \
    "${vite_bin}" \
      "${frontend_root}" \
      --host "${frontend_host}" \
      --port "${frontend_port}" \
      --strictPort &
  frontend_pid="$!"
fi

wait_for_children() {
  local rc
  while true; do
    if ! kill -0 "${api_pid}" >/dev/null 2>&1; then
      set +e
      wait "${api_pid}"
      rc="$?"
      set -e
      api_pid=""
      echo "DataBridge API process exited with status ${rc}." >&2
      return "${rc}"
    fi
    if [[ -n "${frontend_pid}" ]] && ! kill -0 "${frontend_pid}" >/dev/null 2>&1; then
      set +e
      wait "${frontend_pid}"
      rc="$?"
      set -e
      frontend_pid=""
      echo "DataBridge frontend process exited with status ${rc}." >&2
      if [[ "${rc}" == "0" ]]; then
        rc=1
      fi
      return "${rc}"
    fi
    sleep 1
  done
}

wait_for_children
