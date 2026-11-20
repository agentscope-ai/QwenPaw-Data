# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from qwenpaw_data.host.core.utils.safe_name import require_safe_name

MAX_SIZE_BYTES = 20 * 1024 * 1024


def write_file_to_dir(dest_dir: Path, filename: str, data: bytes) -> str:
    name = require_safe_name(filename)
    if not data:
        raise ValueError("file is empty")
    if len(data) > MAX_SIZE_BYTES:
        raise ValueError("file exceeds 20MB")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    try:
        # Exclusive creation also rejects symlinks and concurrent duplicate uploads.
        with dest.open('xb') as stream:
            stream.write(data)
    except FileExistsError:
        raise ValueError(f"duplicate filename: {name}")
    return name
