"""Bind an offline recovery plan to its intended database, not just a URL."""

from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import re
import stat
from uuid import uuid4

from sqlalchemy import insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from qwenpaw_data.host.core.db.tables import LegacyImportTargetRow

from .legacy_json import MigrationError, _canonical
from .target import _check_schema


_TARGET = LegacyImportTargetRow.__table__


def assert_target_anchor(conn, expected: str) -> None:
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{32}", expected):
        raise MigrationError("A valid recorded migration database token is required")
    _check_schema(conn, {_TARGET.name})
    actual = conn.scalar(select(_TARGET.c.token).where(_TARGET.c.id == "target"))
    if (
        not isinstance(actual, str)
        or not re.fullmatch(r"[0-9a-f]{32}", actual)
        or not hmac.compare_digest(actual, expected)
    ):
        raise MigrationError(
            "Migration database token is absent or different; do not infer an uncommitted import or clean up files"
        )


def ensure_target_anchor(database_url: str, *, target_stopped: bool = False) -> str:
    """Explicit metadata write before file work; never create/repair schema.

    A fresh database at the same URL cannot masquerade as an uncommitted run.
    A cloned database can contain this token too: intentional database restores
    still require operator review, not automatic physical-instance claims.
    """
    from .sql_import import _writer

    if target_stopped is not True:
        raise MigrationError(
            "Recording a target database token requires stopped writers"
        )
    with _writer(database_url) as conn:
        _check_schema(conn, {_TARGET.name})
        actual = conn.scalar(select(_TARGET.c.token).where(_TARGET.c.id == "target"))
        if actual is None:
            actual = uuid4().hex
            conn.execute(
                insert(_TARGET),
                dict(id="target", token=actual, created_at=datetime.now(timezone.utc)),
            )
            conn.commit()
        elif not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{32}", actual):
            raise MigrationError(
                "Existing migration database token is invalid; it will not be replaced"
            )
        return actual


def database_binding(database_url: str, key: bytes) -> str:
    """Opaque DSN/path binding; URL passwords are neither retained nor hashed.

    Password rotation is allowed. Host/user/database/options or SQLite inode
    changes require review. No connection is opened, including after a crash.
    The separately persisted database token MUST also be checked in SQL.
    """
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    try:
        if (
            not isinstance(database_url, str)
            or not database_url
            or len(database_url) > 16384
        ):
            raise ValueError
        url = make_url(database_url)
        if url.drivername in {"sqlite", "sqlite+pysqlite"}:
            if (
                url.query
                or url.host
                or url.username
                or url.password
                or not url.database
            ):
                raise ValueError
            path = Path(url.database)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError
            identity = {
                "backend": "sqlite",
                "path": str(path.resolve(strict=True)),
                "device": info.st_dev,
                "inode": info.st_ino,
            }
        elif url.drivername in {"postgresql", "postgresql+psycopg"}:
            if not url.host or not url.username or not url.database:
                raise ValueError
            identity = {
                "backend": "postgresql",
                "host": url.host.lower(),
                "port": url.port or 5432,
                "user": url.username,
                "database": url.database,
                "query": {
                    name: value
                    for name, value in url.query.items()
                    if name not in {"password", "passfile", "sslpassword"}
                },
            }
        else:
            raise ValueError
        return hmac.new(
            key,
            b"qwenpaw-migration-database-v1\0" + _canonical(identity),
            hashlib.sha256,
        ).hexdigest()
    except (ValueError, TypeError, OSError, SQLAlchemyError):
        raise MigrationError(
            "Cannot bind the explicitly selected migration database"
        ) from None
