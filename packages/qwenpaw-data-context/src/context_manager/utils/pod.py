"""Identify the process that currently owns an in-flight row."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def current_pod_name() -> str:
    """Return a stable name for the process that owns an in-flight row.

    Prefer ``POD_NAME``, then the hostname. The process id is the last
    fallback so an ownership comparison never has to match an empty string.
    """
    name = (os.environ.get("POD_NAME") or "").strip()
    if not name:
        name = socket.gethostname().strip()
    if not name:
        log.warning("executor pod name is unavailable; using the process id")
        name = f"pid-{os.getpid()}"
    return name
