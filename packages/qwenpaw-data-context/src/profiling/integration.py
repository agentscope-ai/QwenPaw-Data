"""把 profiling 路由挂进宿主 FastAPI app（独立版 main 与合并版 CM 共用）。

- 路由前缀已是 `/api/semantic-config/probe/*`，直接挂载即可；错误协议复用 semantic_config
  在 `/api/semantic-config` 前缀上的分派（无需在此重复注册异常处理器）。
- 建表用 `profiling.db.init_probe_db`，与 `semantic_config.init_db` 在相同的启动/请求钩子并列调用。
"""

from __future__ import annotations

from fastapi import FastAPI

from profiling.routers import probe_router


def mount_profiling(app: FastAPI) -> None:
    app.include_router(probe_router.router)
