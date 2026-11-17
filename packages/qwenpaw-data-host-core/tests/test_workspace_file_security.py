import asyncio
import os

import pytest

from qwenpaw_data.host.core.api.routers.artifact import resolve_session_file
from qwenpaw_data.host.core.api.workspace_file import open_resolved_workspace_file
from qwenpaw_data.host.core.api.pinned_file_response import PinnedFileResponse


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_replaced_directory_cannot_redirect_open(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_text("outside")
    checked = resolve_session_file(root, "reports/report.txt")
    directory.rename(root / "original")
    _symlink_or_skip(directory, outside)
    with pytest.raises(OSError):
        open_resolved_workspace_file(root, checked)


@pytest.mark.skipif(
    os.name == "nt", reason="Windows may not permit renaming an open file"
)
def test_open_handle_stays_bound_when_directory_is_replaced(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_text("outside")
    checked = resolve_session_file(root, "reports/report.txt")
    with open_resolved_workspace_file(root, checked) as file:
        directory.rename(root / "original")
        _symlink_or_skip(directory, outside)
        assert file.read() == b"inside"


def test_resolved_in_workspace_symlink_can_be_read(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    _symlink_or_skip(root / "alias", directory)
    checked = resolve_session_file(root, "alias/report.txt")
    with open_resolved_workspace_file(root, checked) as file:
        assert file.read() == b"inside"


def test_unresolved_parent_components_are_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("outside")
    with pytest.raises(OSError):
        open_resolved_workspace_file(root, root / ".." / "outside.txt")


def test_symlink_loop_is_a_client_error(tmp_path):
    from fastapi import HTTPException

    root = tmp_path / "workspace"
    root.mkdir()
    try:
        (root / "loop").symlink_to("loop")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(HTTPException) as error:
        resolve_session_file(root, "loop")
    assert error.value.status_code in (400, 404)


@pytest.mark.parametrize(
    ("range_header", "expected_status", "expected_body"),
    [
        (None, 200, b"0123456789"),
        ("bytes=2-5", 206, b"2345"),
        ("bytes=-3", 206, b"789"),
    ],
)
def test_pinned_response_preserves_range_and_closes_handle(
    tmp_path, range_header, expected_status, expected_body
):
    path = tmp_path / "report.txt"
    path.write_bytes(b"0123456789")
    file = open_resolved_workspace_file(tmp_path, path)
    response = PinnedFileResponse(file, path=str(path), filename="report.txt")
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    headers = [(b"range", range_header.encode())] if range_header else []
    scope = {
        "type": "http",
        "method": "GET",
        "headers": headers,
        "extensions": {"http.response.pathsend": {}},
    }
    asyncio.run(response(scope, receive, send))
    assert messages[0]["status"] == expected_status
    assert b"".join(message.get("body", b"") for message in messages) == expected_body
    assert not any(message["type"] == "http.response.pathsend" for message in messages)
    assert file.closed


@pytest.mark.skipif(
    os.name == "nt", reason="Windows may not permit renaming an open file"
)
def test_pinned_response_ignores_path_replacement(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "report.txt"
    path.write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    file = open_resolved_workspace_file(root, path)
    response = PinnedFileResponse(file, path=str(path), filename=path.name)
    path.rename(root / "original.txt")
    path.symlink_to(outside)
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(
        response(
            {"type": "http", "method": "GET", "headers": [], "extensions": {}},
            receive,
            send,
        )
    )
    assert b"".join(message.get("body", b"") for message in messages) == b"inside"
    assert file.closed


def test_pinned_response_serves_multiple_ranges_and_closes_after_send_error(tmp_path):
    path = tmp_path / "report.txt"
    path.write_bytes(b"0123456789")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def scenario():
        file = open_resolved_workspace_file(tmp_path, path)
        response = PinnedFileResponse(file, path=str(path), filename=path.name)
        messages = []

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "headers": [(b"range", b"bytes=0-1,8-9")],
            "extensions": {},
        }
        await response(scope, receive, send)
        body = b"".join(message.get("body", b"") for message in messages)
        assert messages[0]["status"] == 206
        assert (
            b"Content-Range: bytes 0-1/10" in body
            and b"Content-Range: bytes 8-9/10" in body
        )
        assert b"\r\n\r\n01\r\n" in body and b"\r\n\r\n89\r\n" in body
        headers = dict(messages[0]["headers"])
        assert int(headers[b"content-length"]) == len(body)
        assert file.closed

        file = open_resolved_workspace_file(tmp_path, path)
        response = PinnedFileResponse(file, path=str(path), filename=path.name)

        async def disconnected(_message):
            raise RuntimeError("client disconnected")

        with pytest.raises(RuntimeError, match="client disconnected"):
            await response(scope, receive, disconnected)
        assert file.closed

    asyncio.run(scenario())
