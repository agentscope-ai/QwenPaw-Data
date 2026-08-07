"""Access log filesystem writes run on the dedicated writer thread."""

from __future__ import annotations

import logging

from datapaw.context.async_logging import AsyncRotatingFileHandler


def test_async_rotating_handler_flushes_without_business_body(tmp_path):
    path = tmp_path / "access.log"
    handler = AsyncRotatingFileHandler(
        path,
        max_bytes=1024 * 1024,
        backup_count=1,
        queue_size=4,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.Logger("test.access", level=logging.INFO)
    logger.addHandler(handler)

    handler.start()
    logger.info("GET /api/health → 200 3ms")
    handler.stop()

    assert path.read_text(encoding="utf-8") == "GET /api/health → 200 3ms\n"
    assert handler.queued == 0
    assert handler.dropped == 0


def test_writer_initialization_failure_does_not_hang_shutdown(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("occupied", encoding="utf-8")
    handler = AsyncRotatingFileHandler(
        parent / "access.log",
        max_bytes=1024,
        backup_count=1,
        queue_size=1,
    )
    logger = logging.Logger("test.access.failure", level=logging.INFO)
    logger.addHandler(handler)

    handler.start()
    logger.info("this line cannot be written")
    handler.stop()

    assert handler.writer_error is not None
    assert handler.queued == 0
