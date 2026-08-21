#!/usr/bin/env bash

_qwenpaw_data_env_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${QWENPAW_DATA_REPO_ROOT:-}" ]]; then
  QWENPAW_DATA_REPO_ROOT="$(cd "${_qwenpaw_data_env_script_dir}/.." && pwd)"
fi
export QWENPAW_DATA_REPO_ROOT

qwenpaw_data_abs_path() {
  local path="$1"
  case "${path}" in
    "~")
      printf '%s\n' "${HOME}"
      ;;
    "~/"*)
      printf '%s/%s\n' "${HOME}" "${path#\~/}"
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
    chmod 600 "${env_file}"
  fi
}

generate_qwenpaw_data_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
    return
  fi
  echo "openssl or python3 is required to generate local credentials." >&2
  return 1
}

ensure_qwenpaw_data_neo4j_password() {
  local env_file
  env_file="$(qwenpaw_data_env_file)"
  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi
  if ! grep -Eq '^NEO4J_PASSWORD=(YOUR_PASSWORD)?$' "${env_file}"; then
    return 0
  fi

  local generated tmp_file line
  generated="$(generate_qwenpaw_data_secret)"
  tmp_file="$(mktemp "${TMPDIR:-/tmp}/qwenpaw-data-env.XXXXXX")"
  (
    umask 077
    while IFS= read -r line || [[ -n "${line}" ]]; do
      if [[ "${line}" == NEO4J_PASSWORD=YOUR_PASSWORD || "${line}" == NEO4J_PASSWORD= ]]; then
        printf 'NEO4J_PASSWORD=%s\n' "${generated}"
      else
        printf '%s\n' "${line}"
      fi
    done < "${env_file}" > "${tmp_file}"
  )
  mv "${tmp_file}" "${env_file}"
  chmod 600 "${env_file}"
  echo "Generated a random local Neo4j password in ${env_file}."
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
