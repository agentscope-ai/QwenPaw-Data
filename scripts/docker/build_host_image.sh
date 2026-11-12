#!/usr/bin/env bash
# Build and optionally push the standalone QwenPawData Host worker image.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/docker/build_host_image.sh [options]

Options:
  --image IMAGE      Image name/tag. Default: qwenpaw-data-cloud-host:<git-sha>
  --push             Push the image after a successful self-check
  --no-check         Skip the import and readiness self-check
  -h, --help         Show this help

Environment overrides:
  HOST_IMAGE, HOST_BASE_IMAGE, DOCKER_BUILD_NETWORK,
  DOCKER_PIP_INDEX, DOCKER_PIP_TRUSTED_HOST, DOCKER_APT_MIRROR
EOF
}

short_sha="$(git -C "${repo_root}" rev-parse --short=12 HEAD)"
full_sha="$(git -C "${repo_root}" rev-parse HEAD)"
image="${HOST_IMAGE:-qwenpaw-data-cloud-host:${short_sha}}"
push=false
check=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      [[ $# -ge 2 ]] || { echo "--image requires a value" >&2; exit 2; }
      image="$2"
      shift 2
      ;;
    --push)
      push=true
      shift
      ;;
    --no-check)
      check=false
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

command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}

docker build \
  --network "${DOCKER_BUILD_NETWORK:-default}" \
  --build-arg "BASE_IMAGE=${HOST_BASE_IMAGE:-python:3.12-slim-bookworm}" \
  --build-arg "PIP_INDEX_URL=${DOCKER_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}" \
  --build-arg "PIP_TRUSTED_HOST=${DOCKER_PIP_TRUSTED_HOST:-mirrors.aliyun.com}" \
  --build-arg "APT_MIRROR=${DOCKER_APT_MIRROR:-mirrors.aliyun.com}" \
  --build-arg "SOURCE_REVISION=${full_sha}" \
  --tag "${image}" \
  --file "${script_dir}/Host.Dockerfile" \
  "${repo_root}"

if [[ "${check}" == true ]]; then
  echo "Checking Host app import..."
  docker run --rm --entrypoint /opt/qwenpaw-data-cloud/.venv/bin/python "${image}" \
    -c "from qwenpaw_data.host.core.api.app import app; assert app is not None"

  container="qwenpaw-data-host-image-check-$$"
  cleanup() {
    docker rm -f "${container}" >/dev/null 2>&1 || true
  }
  trap cleanup EXIT INT TERM

  docker run -d --name "${container}" \
    -p "127.0.0.1::8089" \
    -e QWENPAW_DATA_HOME=/tmp/qwenpaw-data \
    -e QWENPAW_DATA_DB_URL=sqlite+aiosqlite:////tmp/qwenpaw_data/host.sqlite3 \
    -e QWENPAW_DATA_ENV=development \
    "${image}" >/dev/null

  host_port="$(
    docker port "${container}" 8089/tcp \
      | awk -F: 'NR == 1 { print $NF }'
  )"
  [[ -n "${host_port}" ]] || {
    echo "Could not resolve the self-check host port" >&2
    exit 1
  }
  ready=false
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${host_port}/api/auth/status" >/dev/null 2>&1; then
      ready=true
      break
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" != "true" ]]; then
      break
    fi
    sleep 1
  done
  if [[ "${ready}" != true ]]; then
    docker logs "${container}" >&2 || true
    echo "Host image readiness self-check failed" >&2
    exit 1
  fi
  cleanup
  trap - EXIT INT TERM
fi

if [[ "${push}" == true ]]; then
  docker push "${image}"
fi

image_id="$(docker image inspect -f '{{.Id}}' "${image}")"
echo "HOST_IMAGE=${image}"
echo "HOST_IMAGE_ID=${image_id}"
echo "SOURCE_REVISION=${full_sha}"
