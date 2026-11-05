"""Session-local SQL result references for downstream tools.

The model passes a result_id; consumers load the original query metadata and
verify the CSV digest. No API tokens or preview rows are stored in the record.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

_RESULT_ID = re.compile(r"sql_[0-9a-f]{32}\Z")
_FIELDS = (
    "exec_status",
    "sql",
    "columns",
    "truncated",
    "total_row_count",
    "session_ref",
    "elapsed_ms",
)


def _inside(root: Path, relative: Path) -> Path:
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("SQL result path must remain inside the session")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("SQL result paths cannot contain symbolic links")
    if not path.resolve().is_relative_to(root):
        raise ValueError("SQL result path escaped the session")
    return path


def save_sql_result(payload: dict, *, artifact_dir: Path | str, csv_path: Path) -> str:
    root = Path(artifact_dir).resolve()
    relative = csv_path.relative_to(root)
    source = _inside(root, relative)
    result_id = "sql_" + uuid4().hex
    record_path = _inside(root, Path("data/results") / f"{result_id}.json")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "result_id": result_id,
        "csv": relative.as_posix(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "metadata": {k: payload[k] for k in _FIELDS if k in payload},
    }
    # Exclusive creation prevents a result reference from being overwritten.
    with record_path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False)
    return result_id


def load_sql_result(result_id: str, *, artifact_dir: Path | str) -> dict:
    """Resolve only a record from the supplied current-session artifact root."""
    if not _RESULT_ID.fullmatch(result_id):
        raise ValueError("Invalid SQL result_id")
    root = Path(artifact_dir).resolve()
    path = _inside(root, Path("data/results") / f"{result_id}.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("SQL result_id is not available in this session") from exc
    if record.get("version") != 1 or record.get("result_id") != result_id:
        raise ValueError("Invalid SQL result record")
    source = _inside(root, Path(record["csv"]))
    if (
        not source.is_file()
        or hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]
    ):
        raise ValueError("SQL result CSV is missing or has changed")
    return {**record["metadata"], "file_path": str(source), "result_id": result_id}
