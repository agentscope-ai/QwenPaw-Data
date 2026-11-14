#!/usr/bin/env bash

_qwenpaw_data_env_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${QWENPAW_DATA_REPO_ROOT:-}" ]]; then
  QWENPAW_DATA_REPO_ROOT="$(cd "${_qwenpaw_data_env_script_dir}/../.." && pwd)"
fi
export QWENPAW_DATA_REPO_ROOT

qwenpaw_data_abs_path() {
  local path="$1"
  case "${path}" in
    "~")
      printf '%s\n' "${HOME}"
      ;;
    "~/"*)
      printf '%s/%s\n' "${HOME}" "${path#~/}"
      ;;
    /*)
      printf '%s\n' "${path}"
      ;;
    *)
      printf '%s/%s\n' "$(pwd)" "${path}"
      ;;
  esac
}

qwenpaw_data_env_file() {
  if [[ -n "${QWENPAW_DATA_ENV_FILE:-}" ]]; then
    qwenpaw_data_abs_path "${QWENPAW_DATA_ENV_FILE}"
    return
  fi
  printf '%s/.env\n' "${QWENPAW_DATA_REPO_ROOT}"
}

qwenpaw_data_truthy() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_qwenpaw_data_env_file() {
  local env_file
  env_file="$(qwenpaw_data_env_file)"
  local default_env_file="${QWENPAW_DATA_REPO_ROOT}/.env"
  if [[ "${env_file}" != "${default_env_file}" ]]; then
    return 0
  fi

  local env_example="${QWENPAW_DATA_REPO_ROOT}/.env.example"
  if [[ ! -f "${env_file}" && -f "${env_example}" ]]; then
    cp "${env_example}" "${env_file}"
  fi
}

load_qwenpaw_data_env() {
  local env_file
  env_file="$(qwenpaw_data_env_file)"
  export QWENPAW_DATA_ENV_FILE="${env_file}"

  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi

  local had_allexport=0
  local had_errexit=0
  local had_nounset=0
  case "$-" in *a*) had_allexport=1 ;; esac
  case "$-" in *e*) had_errexit=1; set +e ;; esac
  case "$-" in *u*) had_nounset=1; set +u ;; esac

  set -a
  # shellcheck disable=SC1090
  . "${env_file}"
  local rc=$?

  if [[ "${had_allexport}" != "1" ]]; then
    set +a
  fi
  if [[ "${had_nounset}" == "1" ]]; then
    set -u
  fi
  if [[ "${had_errexit}" == "1" ]]; then
    set -e
  fi
  export QWENPAW_DATA_ENV_FILE="${env_file}"
  return "${rc}"
}

qwenpaw_data_graph_backend() {
  local backend
  backend="$(printf '%s' "${GRAPH_BACKEND:-neo4j}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  case "${backend}" in
    neo4j) printf 'neo4j\n' ;;
    holo_age|holoage|holo-age) printf 'holo_age\n' ;;
    *)
      echo "GRAPH_BACKEND must be neo4j or holo_age, got: ${GRAPH_BACKEND:-}" >&2
      return 2
      ;;
  esac
}

qwenpaw_data_require_holo_age_env() {
  local missing=()
  if [[ -z "${HOLO_AGE_HOST:-}" ]]; then missing+=("HOLO_AGE_HOST"); fi
  if [[ -z "${HOLO_AGE_USER:-}" ]]; then missing+=("HOLO_AGE_USER"); fi
  if [[ -z "${HOLO_AGE_PASSWORD:-}" ]]; then missing+=("HOLO_AGE_PASSWORD"); fi
  if [[ -z "${HOLO_AGE_DATABASE:-}" ]]; then missing+=("HOLO_AGE_DATABASE"); fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "GRAPH_BACKEND=holo_age requires: ${missing[*]}" >&2
    return 2
  fi
}

qwenpaw_data_require_free_port() {
  local label="$1"
  local port="$2"
  local pids

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof is required to check port ${port}." >&2
    return 1
  fi

  pids="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi

  echo "${label} port ${port} is already in use. Stop its owning launcher or choose another port." >&2
  return 1
}

qwenpaw_data_ensure_docker_running() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker CLI was not found. Install Docker for local Neo4j." >&2
    return 1
  fi
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
    colima start
    local attempt
    for attempt in 1 2 3 4 5; do
      if docker info >/dev/null 2>&1; then
        echo "Docker daemon is ready."
        return
      fi
      sleep 2
    done

    echo "Colima is running but Docker is unreachable. Restarting Colima..."
    colima stop
    colima start
  else
    cat >&2 <<'EOF'
Docker CLI is installed, but no Docker daemon is available.
Install and start one of:
  brew install --cask docker
  brew install colima docker && colima start
Then rerun bash scripts/start.sh.
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
