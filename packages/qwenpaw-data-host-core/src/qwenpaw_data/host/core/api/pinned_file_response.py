"""Stream an already opened file while retaining FileResponse range semantics."""

from __future__ import annotations

import os
from secrets import token_hex
from typing import BinaryIO

import anyio
from fastapi.responses import FileResponse
from starlette.datastructures import MutableHeaders


class PinnedFileResponse(FileResponse):
    def __init__(self, file: BinaryIO, *, path: str, **kwargs) -> None:
        self._file = file
        super().__init__(path=path, stat_result=os.fstat(file.fileno()), **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._file.close()

    async def _read(self, size: int) -> bytes:
        return await anyio.to_thread.run_sync(self._file.read, size)

    async def _seek(self, offset: int) -> None:
        await anyio.to_thread.run_sync(self._file.seek, offset)

    async def _handle_simple(
        self, send, send_header_only: bool, send_pathsend: bool
    ) -> None:
        # pathsend would reopen a pathname after validation; always use the handle.
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if not send_header_only:
            while chunk := await self._read(self.chunk_size):
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_single_range(
        self, send, start: int, end: int, file_size: int, send_header_only: bool
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if not send_header_only:
            await self._seek(start)
            while start < end:
                chunk = await self._read(min(self.chunk_size, end - start))
                if not chunk:
                    raise OSError("workspace file changed during download")
                start += len(chunk)
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_multiple_ranges(
        self,
        send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = token_hex(13)
        length, header = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"]
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(length)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if not send_header_only:
            for start, end in ranges:
                await send(
                    {
                        "type": "http.response.body",
                        "body": header(start, end),
                        "more_body": True,
                    }
                )
                await self._seek(start)
                while start < end:
                    chunk = await self._read(min(self.chunk_size, end - start))
                    if not chunk:
                        raise OSError("workspace file changed during download")
                    start += len(chunk)
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
                await send(
                    {"type": "http.response.body", "body": b"\r\n", "more_body": True}
                )
            await send(
                {
                    "type": "http.response.body",
                    "body": f"--{boundary}--".encode("latin-1"),
                    "more_body": False,
                }
            )
        else:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
