#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/start_context.sh [options]

Start qwenpaw-data-context API and Context frontend.
When GRAPH_BACKEND=neo4j (default), also start the local Neo4j container.
When GRAPH_BACKEND=holo_age, skip Docker and use the shared Holo AGE instance.
Run scripts/init_context.sh first on a fresh checkout.

Options:
  --host HOST                 Bind host for context service. Default: 127.0.0.1
  --port PORT                 Bind port for context service. Default: 8765
  --log-level LEVEL           Log level for uvicorn. Default: info
  --reload                    Enable context service reload mode
  --skip-frontend             Start only the graph store and Context API
  -h, --help                  Show this help

Environment:
  QWENPAW_DATA_ENV_FILE           Optional dotenv file. Default: repository root .env
  CONTEXT_HOST                Bind host when --host is omitted
  CONTEXT_PORT                Bind port when --port is omitted
  CONTEXT_LOG_LEVEL           Log level when --log-level is omitted
  DOCKER_START_TIMEOUT        Seconds to wait after opening Docker Desktop on macOS. Default: 120
  CONTEXT_PYTHON              Python executable. Default: packages/qwenpaw-data-context/.venv/bin/python

The Context frontend binds to 127.0.0.1:${CONTEXT_FRONTEND_PORT:-3000} with Vite strictPort enabled.
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

# shellcheck source=scripts/common/env.sh
source "${script_dir}/common/env.sh"
load_qwenpaw_data_env
frontend_port="${CONTEXT_FRONTEND_PORT:-3000}"

host="${CONTEXT_HOST:-127.0.0.1}"
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
    echo "Context Python executable not found: ${CONTEXT_PYTHON}" >&2
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
    --with-db)
      # Kept for compatibility. Local Neo4j starts only when GRAPH_BACKEND=neo4j.
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
  echo "Context Python environment not found: ${python_cmd}" >&2
  echo "Run scripts/init_context.sh first, or set CONTEXT_PYTHON to a prepared interpreter." >&2
  exit 1
fi
if [[ "${skip_frontend}" != "1" ]]; then
  if ! command -v node >/dev/null 2>&1; then
    echo "Node.js was not found. Install Node.js or use --skip-frontend for API-only mode." >&2
    exit 1
  fi
  vite_bin="${frontend_root}/node_modules/.bin/vite"
  if [[ ! -x "${vite_bin}" ]]; then
    echo "Context frontend dependencies are missing: ${vite_bin}" >&2
    echo "Run scripts/init_context.sh first, or use --skip-frontend for API-only mode." >&2
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

start_databases() {
  local backend
  backend="$(qwenpaw_data_graph_backend)" || exit $?
  if [[ "${backend}" == "holo_age" ]]; then
    qwenpaw_data_require_holo_age_env || exit $?
    echo "Graph backend: holo_age (${HOLO_AGE_HOST}:${HOLO_AGE_PORT:-8099}/${HOLO_AGE_DATABASE} graph=${HOLO_AGE_GRAPH_NAME:-cm_graph})"
    return
  fi

  local env_args=()
  if [[ -f "${QWENPAW_DATA_ENV_FILE:-}" ]]; then
    env_args=(--env-file "${QWENPAW_DATA_ENV_FILE}")
  fi
  echo "Graph backend: neo4j (starting local container)"
  qwenpaw_data_ensure_docker_running
  local compose
  if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  else
    echo "Docker Compose was not found. Install Docker Compose to start local Neo4j." >&2
    exit 1
  fi

  local http_port="${NEO4J_HTTP_PORT:-7474}" bolt_port="${NEO4J_BOLT_PORT:-7687}"
  local project="${COMPOSE_PROJECT_NAME:-}" legacy
  if [[ -z "${project}" ]]; then
    project="qwenpaw-data-context-${http_port}-${bolt_port}"
    # Keep the original volume for the default ports and for an existing
    # legacy container on matching custom ports.
    legacy="$(docker container inspect --format \
      '{{index .Config.Labels "com.docker.compose.project"}}:{{(index (index .HostConfig.PortBindings "7474/tcp") 0).HostPort}}:{{(index (index .HostConfig.PortBindings "7687/tcp") 0).HostPort}}' \
      qwenpaw-data-neo4j 2>/dev/null)" || legacy=""
    if [[ "${legacy}" == "qwenpaw-data-context:${http_port}:${bolt_port}" ||
          ( "${legacy}" != qwenpaw-data-context:* && "${http_port}:${bolt_port}" == "7474:7687" ) ]]; then
      project=qwenpaw-data-context
    fi
  fi

  run "${compose[@]}" ${env_args[@]+"${env_args[@]}"} --project-name "${project}" \
    -f "${context_root}/docker-compose.yml" up -d --no-recreate
}

qwenpaw_data_require_free_port "Context API" "${port}"
if [[ "${skip_frontend}" != "1" ]]; then
  qwenpaw_data_require_free_port "Context frontend" "${frontend_port}"
fi

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

printf '+'
printf ' %q' "${cmd[@]}"
printf ' &\n'
"${cmd[@]}" &
api_pid="$!"

if [[ "${skip_frontend}" != "1" ]]; then
  context_url="http://$(url_host "${host}"):${port}"
  printf '+ (cd %q && VITE_API_BASE_URL=%q SERVICE_BASE_URL=%q' "${frontend_root}" "" "${context_url}"
  printf ' %q' \
    "${vite_bin}" \
    --host "127.0.0.1" \
    --port "${frontend_port}" \
    --strictPort
  printf ')\n'
  # NOTE: run vite from within frontend_root WITHOUT a positional root arg.
  # Passing frontend_root as a positional `root` breaks module transforms when
  # the repo is reached through a symlink (the resolved id lands on the real
  # path and is rejected as "outside root", so Vite falls back to serving raw
  # .tsx and the browser fails to parse the TS `!` / JSX).
  (
    cd "${frontend_root}"
    exec env VITE_API_BASE_URL="" SERVICE_BASE_URL="${context_url}" \
      "${vite_bin}" \
        --host "127.0.0.1" \
        --port "${frontend_port}" \
        --strictPort
  ) &
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
      echo "Context API process exited with status ${rc}." >&2
      return "${rc}"
    fi
    if [[ -n "${frontend_pid}" ]] && ! kill -0 "${frontend_pid}" >/dev/null 2>&1; then
      set +e
      wait "${frontend_pid}"
      rc="$?"
      set -e
      frontend_pid=""
      echo "Context frontend process exited with status ${rc}." >&2
      if [[ "${rc}" == "0" ]]; then
        rc=1
      fi
      return "${rc}"
    fi
    sleep 1
  done
}

wait_for_children
