#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify.sh [options]

Run the standard verification pipeline for the repository:
  1. python -m compileall over packages/, scripts/, and examples/
  2. uv run pytest -q
  3. API server smoke test (start serve.py, curl /api/health, stop)

Options:
  --frontend                  Also run frontend lint and build (npm)
  --skip-smoke                Skip the API server smoke test
  --smoke-port PORT           Port for the smoke test server. Default: 8799
  -h, --help                  Show this help
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
context_root="${repo_root}/packages/qwenpaw-data-context"
frontend_root="${context_root}/frontend"

run_frontend=0
skip_smoke=0
smoke_port=8799

while [[ $# -gt 0 ]]; do
  case "$1" in
    --frontend)
      run_frontend=1
      shift
      ;;
    --skip-smoke)
      skip_smoke=1
      shift
      ;;
    --smoke-port)
      smoke_port="${2:?--smoke-port requires a value}"
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

step() {
  printf '\n==> %s\n' "$1"
}

fail() {
  printf 'verify: FAILED at: %s\n' "$1" >&2
  exit 1
}

cd "${repo_root}"

step "compileall (packages/ + scripts/ + examples/)"
uv run python -m compileall -q \
  -x '(\.venv|node_modules|__pycache__)' \
  packages scripts examples || fail "compileall"

step "pytest"
uv run pytest -q || fail "pytest"

if [[ "${skip_smoke}" != "1" ]]; then
  step "API smoke test on 127.0.0.1:${smoke_port}"
  smoke_log="$(mktemp -t qwenpaw-data-verify-smoke)"
  uv run python "${context_root}/scripts/serve.py" \
    --host 127.0.0.1 --port "${smoke_port}" --log-level warning \
    >"${smoke_log}" 2>&1 &
  smoke_pid="$!"
  cleanup_smoke() {
    if kill -0 "${smoke_pid}" 2>/dev/null; then
      kill "${smoke_pid}" 2>/dev/null || true
      wait "${smoke_pid}" 2>/dev/null || true
    fi
  }
  trap cleanup_smoke EXIT

  smoke_ok=0
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${smoke_port}/api/health" >/dev/null 2>&1; then
      smoke_ok=1
      break
    fi
    if ! kill -0 "${smoke_pid}" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if [[ "${smoke_ok}" != "1" ]]; then
    echo "--- smoke server log (tail) ---" >&2
    tail -40 "${smoke_log}" >&2 || true
    fail "API smoke test (/api/health)"
  fi
  cleanup_smoke
  trap - EXIT
  echo "smoke test OK"
fi

if [[ "${run_frontend}" == "1" ]]; then
  step "frontend lint"
  (cd "${frontend_root}" && npm run lint) || fail "npm run lint"
  step "frontend build"
  (cd "${frontend_root}" && npm run build) || fail "npm run build"
fi

printf '\nverify: ALL CHECKS PASSED\n'
