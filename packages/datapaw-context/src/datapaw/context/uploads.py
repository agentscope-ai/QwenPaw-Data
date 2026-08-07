"""上传大小统一限制。

所有接收 ``UploadFile`` 的路由应通过 :func:`read_upload` 读取内容，
按块累计并在超过上限时立即返回 413，避免无上限地整读进内存。
上限由环境变量 ``DATAPAW_MAX_UPLOAD_MB`` 控制（默认 50 MB，
与 KG 文档存储的 ``DOC_MAX_SIZE`` 默认值对齐）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .resource_budget import get_resource_limits

_CHUNK_SIZE = 1024 * 1024  # 1 MB
def max_upload_bytes() -> int:
    """返回当前生效的上传大小上限（字节）。"""
    return get_resource_limits().max_upload_bytes


class _RequestTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """同时校验 Content-Length 和实际 ASGI body 字节数。"""

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self._configured_max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._configured_max_bytes or max_upload_bytes()
        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    await self._reject(scope, receive, send, limit)
                    return
            except ValueError:
                pass

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        limit: int,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": (
                    f"请求体超过大小上限 {limit // (1024 * 1024)} MB"
                    "（可通过 DATAPAW_MAX_UPLOAD_MB 调整）"
                )
            },
        )
        await response(scope, receive, send)


async def read_upload(file: UploadFile, max_bytes: int | None = None) -> bytes:
    """分块读取上传文件，超过 ``max_bytes`` 时抛 413。

    ``max_bytes`` 缺省取 :func:`max_upload_bytes`。
    """
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"上传文件超过大小上限 {limit // (1024 * 1024)} MB"
                    "（可通过 DATAPAW_MAX_UPLOAD_MB 调整）"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def read_upload_sync(file: UploadFile, max_bytes: int | None = None) -> bytes:
    """同步路由使用的分块限额读取器。

    FastAPI 会把普通 ``def`` 路由放入有界线程池；同步 driver、openpyxl、
    OSS 和文件操作所在的路由应使用本函数，避免在事件循环中调用它们。
    """
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"上传文件超过大小上限 {limit // (1024 * 1024)} MB"
                    "（可通过 DATAPAW_MAX_UPLOAD_MB 调整）"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def save_upload_to_temp(
    file: UploadFile,
    *,
    suffix: str = "",
    max_bytes: int | None = None,
) -> Path:
    """把上传内容限额、分块落到临时文件，避免再复制一份完整内容到内存。

    调用方负责在使用后删除返回路径。写入失败或超过限制时，本函数会自行
    清理未完成的文件。
    """
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    fd, tmp_name = tempfile.mkstemp(prefix="datapaw_upload_", suffix=suffix)
    path = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = file.file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"上传文件超过大小上限 {limit // (1024 * 1024)} MB"
                            "（可通过 DATAPAW_MAX_UPLOAD_MB 调整）"
                        ),
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
