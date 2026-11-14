"""POSIX, process-held migration exclusion for one canonical target home.

This is not a Host/application lock: all application writers must be stopped.
The lock file is deliberately never unlinked, avoiding split-inode lock races.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from .bundle import _home_identity
from .legacy_json import MigrationError, _portable_key


_NAME = ".legacy-migration.lock"


@dataclass
class MigrationLock:
    home: Path
    identity: tuple[int, int]
    home_fd: int = field(repr=False)
    lock_fd: int = field(repr=False)
    pid: int = field(repr=False)
    active: bool = field(default=True, repr=False)

    def check(self):
        if not self.active or os.getpid() != self.pid:
            raise MigrationError("Migration lock is no longer held by this process")
        if _home_identity(self.home) != self.identity:
            raise MigrationError("Migration target directory changed while locked")
        opened = os.fstat(self.lock_fd)
        named = os.stat(_NAME, dir_fd=self.home_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
        ):
            raise MigrationError("Migration lock file changed or is unsafe")


@contextmanager
def migration_lock(target_home: Path, *, target_stopped: bool = False):
    if target_stopped is not True:
        raise MigrationError("Migration locking requires stopped target writers")
    if os.name != "posix":
        raise MigrationError(
            "Migration locking requires POSIX; Windows acceptance is pending"
        )
    import fcntl

    home_fd = lock_fd = None
    held = None
    try:
        home = Path(target_home)
        identity = _home_identity(home)
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if (os.fstat(home_fd).st_dev, os.fstat(home_fd).st_ino) != identity:
            raise MigrationError("Migration target directory changed before locking")
        if any(
            name != _NAME and _portable_key(name) == _portable_key(_NAME)
            for name in os.listdir(home_fd)
        ):
            raise MigrationError("Migration lock name has a portable filesystem alias")
        try:
            lock_fd = os.open(
                _NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=home_fd,
            )
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            os.fsync(home_fd)
        except FileExistsError:
            lock_fd = os.open(
                _NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=home_fd
            )
        held = MigrationLock(home, identity, home_fd, lock_fd, os.getpid())
        held.check()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError(
                "Another migration process holds the target home lock"
            ) from None
        held.check()
        yield held
        held.check()
    except OSError:
        raise MigrationError(
            "Cannot safely acquire or retain the migration lock"
        ) from None
    finally:
        if held is not None:
            held.active = False
        if lock_fd is not None:
            # close also releases the OS lock after an exception or process exit.
            os.close(lock_fd)
        if home_fd is not None:
            os.close(home_fd)
