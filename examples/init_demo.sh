#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
compose_file="${script_dir}/docker-compose.yml"
register=0
sqlite_only=0

usage() {
  cat <<'EOF'
Usage: examples/init_demo.sh [--sqlite-only] [--register]

Create the SQLite demo and, by default, start and seed demo PostgreSQL.
--register imports the bundled semantic workbook and configures its fixed
DataBridge datasource against the local PostgreSQL service.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sqlite-only) sqlite_only=1 ;;
    --register) register=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

python3 "${script_dir}/init_demo.py"

if [[ "${sqlite_only}" == "1" ]]; then
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the PostgreSQL demo; use --sqlite-only without Docker." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required; install the Docker Compose plugin." >&2
  exit 1
fi

docker compose -f "${compose_file}" up -d --wait
uv run python "${script_dir}/init_demo.py" \
  --postgres-dsn "postgresql://qwenpaw_data:qwenpaw-data-demo@127.0.0.1:${QWENPAW_DATA_DEMO_POSTGRES_PORT:-55432}/qwenpaw_data_demo"

if [[ "${register}" == "1" ]]; then
  uv run python "${script_dir}/init_demo.py" \
    --register
fi

printf 'Demo ready. Repository root: %s\n' "${repo_root}"
