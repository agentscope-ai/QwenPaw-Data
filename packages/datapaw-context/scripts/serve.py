#!/usr/bin/env python3
"""启动 CM 后端 API 服务（FastAPI + uvicorn）。

本脚本只启动后端 API，不包含前端页面。前端是独立的 Vite + React 项目，
位于 ``frontend/``，需单独 ``npm run dev`` 启动（默认端口 3000）。

用法：

    python scripts/serve.py                 # 默认 http://localhost:8765
    python scripts/serve.py --port 8000
    python scripts/serve.py --reload        # 开发模式：改后端代码自动热重载
    make serve                              # 等价快捷入口

前置：

- 已经跑过 ``scripts/setup/build_topology.py`` 把图谱建好
- 仓库根目录 ``.env`` 里 NEO4J_URI/USER/PASSWORD 配好

启动后访问 API：``http://localhost:8765/docs``（OpenAPI 文档）。
前端页面请另起 ``cd frontend && npm run dev``，默认 ``http://localhost:3000``。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "."))
sys.path.insert(0, str(_repo))


def _is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _authentication_configured() -> bool:
    if (os.environ.get("DATAPAW_API_TOKEN") or "").strip():
        return True
    raw_keys = (os.environ.get("DATAPAW_API_KEYS") or "").strip()
    if not raw_keys:
        return False
    try:
        parsed = json.loads(raw_keys)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, list) and bool(parsed)


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1；对外暴露需显式指定 0.0.0.0 并自行做好访问控制）")
    p.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p.add_argument("--reload", action="store_true", help="开发模式：源码改动自动重启")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.add_argument(
        "--limit-concurrency",
        type=int,
        default=_positive_int_env("DATAPAW_HTTP_MAX_CONCURRENCY", 128),
        help="ASGI 并发请求上限；超限返回 503（默认 128）",
    )
    p.add_argument(
        "--require-auth",
        action="store_true",
        help=(
            "即使 API 仅绑定回环地址也要求配置认证；供对外暴露的前端代理使用"
        ),
    )
    args = p.parse_args()

    if args.limit_concurrency < 1:
        p.error("--limit-concurrency must be >= 1")

    import uvicorn

    # 非回环地址，或由对外暴露的前端反向代理访问时，无 API token 必须
    # fail closed，避免通过 Vite /api proxy 绕过回环绑定保护。
    if (
        args.require_auth or not _is_loopback_host(args.host)
    ) and not _authentication_configured():
        print(
            f"[serve] refusing to start on {args.host}: API authentication is required. "
            "Configure DATAPAW_API_TOKEN or DATAPAW_API_KEYS, or keep both API "
            "and frontend on loopback.",
            file=sys.stderr,
        )
        return 2

    # reload 模式必须用 import string，否则 uvicorn 没法重新加载
    target = "context_manager.api.server:app"
    repo_root = Path(__file__).resolve().parent.parent
    print(f"[serve] API server listening on {args.host}:{args.port}  (Ctrl-C to stop)")
    print(f"[serve] OpenAPI docs: http://localhost:{args.port}/docs")
    print(
        "[serve] 直接运行 serve.py 时，前端需另起: "
        "cd frontend && npm run dev  (默认 http://localhost:3000)"
    )
    uvicorn.run(
        target,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(repo_root / "src")] if args.reload else None,
        log_level=args.log_level,
        limit_concurrency=args.limit_concurrency,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
