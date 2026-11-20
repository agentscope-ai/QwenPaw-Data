#!/usr/bin/env bash
# 全量 pytest —— 本地入口，用例集与 CI 相同（CI 见 .github/workflows/ci.yml）。参数透传给 pytest：
#     bash scripts/tests/run_pytest.sh -v | -k followup | --lf
# 环境是 hermetic 的（见下），别用裸的 uv run pytest 代替。
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common/env.sh
source "${script_dir}/../common/env.sh"
repo_root="${QWENPAW_DATA_REPO_ROOT}"
cd "${repo_root}"

usage() {
  cat <<'EOF'
Usage: scripts/tests/run_pytest.sh [pytest arguments...]

Runs the full test suite (testpaths = packages, tests) the same way CI does.
Extra arguments are forwarded to pytest.

Environment:
  QWENPAW_DATA_PYTHON   Interpreter to use. Default: <repo>/.venv/bin/python
EOF
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

python_bin="${QWENPAW_DATA_PYTHON:-${repo_root}/.venv/bin/python}"
if [[ ! -x "${python_bin}" ]]; then
  cat >&2 <<EOF
找不到可执行的 Python：${python_bin}
先装依赖（按 uv.lock 建 .venv，含 dev 组的 pytest / pytest-asyncio）：
    uv sync --all-packages
uv 认 UV_PROJECT_ENVIRONMENT，.venv 不在仓库里时用 QWENPAW_DATA_PYTHON 指到装了 pytest 的解释器。
EOF
  exit 1
fi

# 一次查全：pytest-asyncio 缺了，asyncio_mode=auto 会以 usage error（退出码 4）崩掉。
if ! "${python_bin}" -c 'import pytest, pytest_asyncio' >/dev/null 2>&1; then
  cat >&2 <<EOF
${python_bin} 里缺 pytest / pytest-asyncio。
    uv sync --all-packages
--all-packages 不能省：根项目是 uv virtual root，裸 uv sync 还会把 workspace 成员的包剪掉。
EOF
  exit 1
fi

# hermetic：不 source .env，也不继承调用者的 QWENPAW_DATA_HOME / QWENPAW_DATA_DB_URL /
# QWENPAW_DATA_AUTH_SECRET / PYTEST_ADDOPTS / PYTEST_PLUGINS，全部钉到一次性目录，
# 免得用例写进开发者的真实运行库、或被调用者的 ADDOPTS 缩成一个子集。
tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/qwenpaw-data-pytest.XXXXXX")"
trap 'rm -rf "${tmp_root}"' EXIT
mkdir -p "${tmp_root}/home"
export QWENPAW_DATA_HOME="${tmp_root}/home"
export QWENPAW_DATA_DB_URL="sqlite+aiosqlite:///${tmp_root}/pytest.db"
unset QWENPAW_DATA_AUTH_SECRET PYTEST_ADDOPTS PYTEST_PLUGINS

# 光 unset 不够：context_manager.config 在 import 期就 load_qwenpaw_data_env()，未设
# QWENPAW_DATA_ENV_FILE 时会回落读 <repo>/.env，把 AUTH_SECRET / NEO4J_* / OPENAI_API_KEY
# 重新灌回来。指向一个空文件即可；上面已 export 的两个变量仍是钉死的值。
export QWENPAW_DATA_ENV_FILE="${tmp_root}/empty.env"
: > "${QWENPAW_DATA_ENV_FILE}"

# 不 exec：exec 会顶掉上面的 EXIT trap，每次运行都漏一个 /tmp/qwenpaw-data-pytest.*。
rc=0
"${python_bin}" -m pytest -q --durations=10 -rfE "$@" || rc=$?

if [[ "${rc}" -ne 0 ]]; then
  cat >&2 <<'EOF'

pytest 未通过（退出码见上）。
只重跑上一轮失败的用例：bash scripts/tests/run_pytest.sh --lf
EOF
fi
exit "${rc}"