"""KG document storage backed by the local filesystem."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import CFG, Config
from .exceptions import DocAlreadyExistsError, DocNotFoundError

_DOC_ID_PREFIX = "kg-docs/"


def _normalize_filename(filename: str) -> str:
    name = Path(filename).name
    if not name or name != filename.strip() or ".." in name or "/" in filename or "\\" in filename:
        raise ValueError(f"invalid filename: {filename!r}")
    return name


def filename_from_doc_id(doc_id: str) -> str:
    normalized = doc_id.strip()
    if normalized.startswith(_DOC_ID_PREFIX):
        normalized = normalized[len(_DOC_ID_PREFIX) :]
    return _normalize_filename(normalized)


def canonical_doc_id(doc_id: str) -> str:
    return f"{_DOC_ID_PREFIX}{filename_from_doc_id(doc_id)}"


@dataclass(frozen=True)
class DocObject:
    doc_id: str
    filename: str
    file_size: int
    last_modified: datetime | None = None


@dataclass(frozen=True)
class DocListItem:
    doc_id: str
    filename: str
    file_size: int
    download_url: str
    last_modified: datetime | None = None


@dataclass(frozen=True)
class DocListResult:
    items: list[DocListItem]
    total: int
    page: int
    page_size: int


class LocalDocClient:
    """Document storage backed by a local directory.

    Public ``doc_id`` values use ``kg-docs/{filename}``, while files are stored
    flat under ``storage_dir/{filename}``.
    """

    def __init__(self, storage_dir: Path, max_size: int) -> None:
        self._storage_dir = storage_dir
        self._max_size = max_size
        storage_dir.mkdir(parents=True, exist_ok=True)

    @property
    def max_size(self) -> int:
        """单个文档的大小上限（字节，来自 DOC_MAX_SIZE）。"""
        return self._max_size

    @classmethod
    def from_config(cls, cfg: Config = CFG) -> LocalDocClient:
        return cls(
            storage_dir=Path(cfg.doc_storage_dir),
            max_size=cfg.doc_max_size,
        )

    def _path(self, doc_id: str) -> Path:
        safe = filename_from_doc_id(doc_id)
        return self._storage_dir / safe

    def upload(
        self,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> DocObject:
        safe_name = _normalize_filename(filename)
        if len(content) > self._max_size:
            raise ValueError("file exceeds max size")
        dest = self._storage_dir / safe_name
        if dest.exists():
            raise DocAlreadyExistsError(safe_name)
        dest.write_bytes(content)
        return DocObject(
            doc_id=canonical_doc_id(safe_name),
            filename=safe_name,
            file_size=len(content),
        )

    def list_docs(self, page: int = 1, page_size: int = 20) -> DocListResult:
        if page < 1:
            raise ValueError("page must be >= 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")

        objects: list[DocObject] = []
        for entry in self._storage_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                continue
            stat = entry.stat()
            objects.append(
                DocObject(
                    doc_id=canonical_doc_id(entry.name),
                    filename=entry.name,
                    file_size=stat.st_size,
                    last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )

        objects.sort(
            key=lambda o: o.last_modified or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        total = len(objects)
        start = (page - 1) * page_size
        page_items = [
            DocListItem(
                doc_id=obj.doc_id,
                filename=obj.filename,
                file_size=obj.file_size,
                download_url="",
                last_modified=obj.last_modified,
            )
            for obj in objects[start : start + page_size]
        ]
        return DocListResult(items=page_items, total=total, page=page, page_size=page_size)

    def file_exists(self, doc_id: str) -> bool:
        return self._path(doc_id).exists()

    def get_file_path(self, doc_id: str) -> Path:
        """Return the absolute path to the stored file; raise ``DocNotFoundError`` if absent."""
        path = self._path(doc_id)
        if not path.exists():
            raise DocNotFoundError(doc_id)
        return path

    def delete(self, doc_id: str) -> str:
        path = self._path(doc_id)
        if not path.exists():
            raise DocNotFoundError(doc_id)
        path.unlink()
        return canonical_doc_id(doc_id)


_client: Optional[LocalDocClient] = None


def get_local_doc_client() -> LocalDocClient:
    global _client
    if _client is None:
        _client = LocalDocClient.from_config()
    return _client
