"""Explicit local-key provisioning, separate from read-only migration planning.

No source access, implicit key generation, runtime settings, account creation,
schema repair or database writes. Environment-based production deployments must
provision their secret out of band; this operation cannot configure a parent
process, another service, or override a deployment's environment variables.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import stat
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, inspect, select

from qwenpaw_data.host.core.db.tables import Base

from .legacy_json import MigrationError
from .target import _read_only_target


@dataclass(frozen=True)
class KeySetup:
    status: str
    encrypted_credentials_checked: int
    plaintext_credentials_found: int

    def report(self) -> dict:
        return {
            "stage": "local_key_provisioning_only",
            "status": self.status,
            "target_key_file_written": self.status == "created_and_verified",
            "target_key_file_matches": True,
            "target_runtime_configuration_verified": False,
            "encrypted_credentials_checked": self.encrypted_credentials_checked,
            "plaintext_credentials_found": self.plaintext_credentials_found,
            "sql_written": False,
            "import_ready": False,
            "warning": "Only the local preferences.key file is configured. Runtime environment secrets take precedence; production requires environment provisioning. Configure every service sharing this database consistently and recheck before import. Existing plaintext credentials are reported, not rewritten. Back up this key separately and keep it private.",
        }


def check_database_credentials(
    conn, key: bytes, *, max_records: int = 100_000
) -> tuple[int, int]:
    """Authenticate every stored provider secret under the shared target key.

    All users share this key, not only users in the source identity mapping.
    Counts only: never return credentials or use runtime's empty-on-error decrypt.
    """
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    if type(max_records) is not int or max_records <= 0:
        raise MigrationError(
            "Target credential record limit must be a positive integer"
        )
    inspector = inspect(conn)
    if "user_providers" not in inspector.get_table_names() or "api_key_enc" not in {
        column["name"] for column in inspector.get_columns("user_providers")
    }:
        raise MigrationError(
            "Initialize the target user_providers schema separately before configuring a key"
        )
    cipher = Fernet(base64.urlsafe_b64encode(key))
    encrypted = plaintext = 0
    table = Base.metadata.tables["user_providers"]
    # Bound each value before transferring it and stream bounded batches from
    # PostgreSQL; a record-count limit alone could still buffer gigabytes.
    query = select(func.substr(table.c.api_key_enc, 1, 65537)).limit(max_records + 1)
    with conn.execute(query.execution_options(yield_per=100)) as result:
        for count, value in enumerate(result.scalars(), 1):
            if count > max_records:
                raise MigrationError(
                    "Target credentials exceed the configured record limit"
                )
            if not isinstance(value, str) or not value.strip() or len(value) > 65536:
                raise MigrationError(
                    "Target contains an invalid or oversized credential"
                )
            try:
                raw = value.encode("utf-8")
                if len(raw) > 65536:
                    raise ValueError
                if value.startswith("ENC:"):
                    decoded = cipher.decrypt(raw[4:]).decode("utf-8")
                    if not decoded.strip():
                        raise ValueError
                    encrypted += 1
                else:
                    plaintext += 1
            except (InvalidToken, UnicodeError, ValueError):
                raise MigrationError(
                    "Cannot authenticate or decode existing target credentials with the selected key"
                ) from None
    return encrypted, plaintext


def _existing_key(directory: int, key: bytes) -> bool:
    try:
        fd = os.open(
            "preferences.key",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return False
    except OSError:
        raise MigrationError(
            "Existing target key must be a private regular non-symlink file"
        ) from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 1024
        ):
            raise MigrationError(
                "Existing target key must be private, owned, regular and without extra hard links"
            )
        value = os.read(fd, 1025)
    finally:
        os.close(fd)
    try:
        text = value.decode("ascii").strip()
        if len(text) != 64:
            raise ValueError
        existing = bytes.fromhex(text)
    except (UnicodeError, ValueError):
        raise MigrationError(
            "Existing target key is malformed; it will not be replaced"
        ) from None
    if not hmac.compare_digest(existing, key):
        raise MigrationError(
            "Existing target key differs from the selected key; it will not be replaced"
        )
    return True


def provision_local_key(
    target_home: Path, *, key: bytes, database_url: str, target_stopped: bool = False
) -> KeySetup:
    """Publish one complete private key without replacing an existing file.

    POSIX only for now: directory-descriptor operations and permissions provide
    the tested ownership/publication boundary. Windows ACL acceptance is pending.
    The home must already exist; no runtime folder is implicitly initialized.
    """
    if target_stopped is not True:
        raise MigrationError("Key provisioning requires stopped target writers")
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    if os.name != "posix":
        raise MigrationError(
            "Local migration key provisioning requires POSIX permission support; configure deployment secrets separately on this platform"
        )
    home = Path(target_home)
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise MigrationError(
            "Key provisioning requires an explicit existing absolute target home without a final symlink"
        )
    directory = None
    temporary = None
    try:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise MigrationError(
                "Target home must be owned by the current user and not group/world writable"
            )
        exists = _existing_key(directory, key)
        with _read_only_target(database_url) as conn:
            encrypted, plaintext = check_database_credentials(conn, key)
        # No writes before both existing key and all database credentials pass.
        if not exists:
            name = f".migration-key-{uuid4().hex}.tmp"
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            temporary = name
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(key.hex().encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    name,
                    "preferences.key",
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # A concurrent publisher must be revalidated, never overwritten.
                exists = True
            os.unlink(name, dir_fd=directory)
            temporary = None
            os.fsync(directory)
        current = home.stat()
        if home.is_symlink() or (current.st_dev, current.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise MigrationError(
                "Target home changed during key provisioning; inspect its original location before retrying"
            )
        if not _existing_key(directory, key):
            raise MigrationError("Target key disappeared during provisioning")
        return KeySetup(
            "already_verified" if exists else "created_and_verified",
            encrypted,
            plaintext,
        )
    except OSError:
        raise MigrationError(
            "Local key provisioning failed; inspect the target key before retrying, never replace it blindly"
        ) from None
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise MigrationError(
                        "Temporary private key cleanup failed; inspect the target home before retrying"
                    ) from None
        finally:
            if directory is not None:
                os.close(directory)
