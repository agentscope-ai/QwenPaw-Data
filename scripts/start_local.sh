#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/start_local.sh [options]

Start the recommended local full-stack runtime:
  local Neo4j, DataBridge API, and DataBridge frontend

Existing listeners on DataBridge frontend port 3000 and the configured DataBridge
API port are stopped first.

Options:
  --context-host HOST         Bind host for DataBridge service. Default: 127.0.0.1
  --context-port PORT         Bind port for DataBridge service. Default: 8765
  --context-log-level LEVEL   DataBridge service log level. Default: info
  --context-reload            Enable DataBridge service reload mode
  --skip-frontend             Start only the database and DataBridge API
  -h, --help                  Show this help

Environment:
  QWENPAW_DATA_ENV_FILE           Optional dotenv file. Default: repository root .env
  CONTEXT_HOST                DataBridge bind host when --context-host is omitted
  CONTEXT_PORT                DataBridge bind port when --context-port is omitted
  CONTEXT_LOG_LEVEL           DataBridge log level when --context-log-level is omitted
  CONTEXT_PYTHON              Python executable for DataBridge service
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

# shellcheck source=scripts/env.sh
source "${script_dir}/env.sh"
load_qwenpaw_data_env

context_host="${CONTEXT_HOST:-127.0.0.1}"
context_port="${CONTEXT_PORT:-8765}"
context_frontend_port="3000"
context_log_level="${CONTEXT_LOG_LEVEL:-info}"
context_reload=0
skip_frontend=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context-host)
      context_host="${2:?--context-host requires a value}"
      shift 2
      ;;
    --context-port)
      context_port="${2:?--context-port requires a value}"
      shift 2
      ;;
    --context-log-level)
      context_log_level="${2:?--context-log-level requires a value}"
      shift 2
      ;;
    --context-reload)
      context_reload=1
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

shutdown_port() {
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

  echo "Stopping existing ${label} listener on port ${port}: ${pids//$'\n'/ }"
  kill -TERM ${pids} >/dev/null 2>&1 || true
  sleep 1

  pids="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "Force stopping ${label} listener on port ${port}: ${pids//$'\n'/ }" >&2
    kill -KILL ${pids} >/dev/null 2>&1 || true
    sleep 0.2
  fi

  if lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Failed to release ${label} port ${port}." >&2
    exit 1
  fi
}

cd "${repo_root}"

shutdown_port "DataBridge API" "${context_port}"
if [[ "${skip_frontend}" != "1" ]]; then
  shutdown_port "DataBridge frontend" "${context_frontend_port}"
fi

context_cmd=(
  "${script_dir}/start_databridge.sh"
  --host "${context_host}"
  --port "${context_port}"
  --log-level "${context_log_level}"
)
if [[ "${context_reload}" == "1" ]]; then
  context_cmd+=(--reload)
fi
if [[ "${skip_frontend}" == "1" ]]; then
  context_cmd+=(--skip-frontend)
fi

run "${context_cmd[@]}"
