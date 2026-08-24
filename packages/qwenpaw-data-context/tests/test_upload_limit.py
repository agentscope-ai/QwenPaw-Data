"""上传大小统一限制（qwenpaw_data.context.uploads）的回归测试。"""

from __future__ import annotations

import io

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient

from qwenpaw_data.context.uploads import (
    RequestBodyLimitMiddleware,
    max_upload_bytes,
    read_upload,
    read_upload_sync,
    save_upload_to_temp,
)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> dict:
        content = await read_upload(file)
        return {"size": len(content)}

    return app


def test_default_limit_is_50mb(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_MAX_UPLOAD_MB", raising=False)
    assert max_upload_bytes() == 50 * 1024 * 1024


def test_env_override_and_invalid_value(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "2")
    assert max_upload_bytes() == 2 * 1024 * 1024
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "not-a-number")
    assert max_upload_bytes() == 50 * 1024 * 1024


def test_upload_within_limit_ok(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "1")
    client = TestClient(_build_app())
    payload = b"x" * (512 * 1024)
    resp = client.post("/upload", files={"file": ("small.bin", io.BytesIO(payload))})
    assert resp.status_code == 200
    assert resp.json()["size"] == len(payload)


def test_upload_over_limit_returns_413(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "1")
    client = TestClient(_build_app())
    payload = b"x" * (1024 * 1024 + 1)
    resp = client.post("/upload", files={"file": ("big.bin", io.BytesIO(payload))})
    assert resp.status_code == 413
    assert "QWENPAW_DATA_MAX_UPLOAD_MB" in resp.json()["detail"]


def test_explicit_max_bytes_beats_env(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "1")
    app = FastAPI()

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> dict:
        content = await read_upload(file, max_bytes=10)
        return {"size": len(content)}

    client = TestClient(app)
    resp = client.post("/upload", files={"file": ("b.bin", io.BytesIO(b"x" * 11))})
    assert resp.status_code == 413


def test_sync_reader_enforces_same_limit(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "1")
    app = FastAPI()

    @app.post("/upload")
    def upload(file: UploadFile = File(...)) -> dict:
        content = read_upload_sync(file)
        return {"size": len(content)}

    client = TestClient(app)
    ok = client.post(
        "/upload",
        files={"file": ("small.bin", io.BytesIO(b"x" * 32))},
    )
    too_large = client.post(
        "/upload",
        files={"file": ("large.bin", io.BytesIO(b"x" * (1024 * 1024 + 1)))},
    )

    assert ok.status_code == 200
    assert ok.json()["size"] == 32
    assert too_large.status_code == 413


def test_streaming_temp_file_reader_cleans_up(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_MAX_UPLOAD_MB", "1")
    app = FastAPI()

    @app.post("/upload")
    def upload(file: UploadFile = File(...)) -> dict:
        path = save_upload_to_temp(file, suffix=".bin")
        try:
            return {"size": path.stat().st_size, "suffix": path.suffix}
        finally:
            path.unlink(missing_ok=True)

    response = TestClient(app).post(
        "/upload",
        files={"file": ("payload.bin", io.BytesIO(b"payload"))},
    )

    assert response.status_code == 200
    assert response.json() == {"size": 7, "suffix": ".bin"}


def test_request_body_middleware_checks_actual_size():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=10)

    @app.post("/body")
    async def body(request: Request) -> dict:
        return {"size": len(await request.body())}

    client = TestClient(app)
    assert client.post("/body", content=b"x" * 10).status_code == 200
    response = client.post("/body", content=b"x" * 11)
    assert response.status_code == 413
