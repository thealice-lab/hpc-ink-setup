from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from inkly.core.permissions import validate_private_acl


def private_directory(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("Private storage must not be a symlink")
    if os.name == "posix":
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PermissionError("Private storage must be owned by its user")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise PermissionError("Private storage must be owned by its user")
            os.fchmod(descriptor, 0o700)
            validate_private_acl(descriptor, directory=True)
        finally:
            os.close(descriptor)
    return directory


def private_open(path: Path, flags: int) -> int:
    if path.is_symlink():
        raise PermissionError("Private files must not be symlinks")
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Private files must be regular, unlinked files")
        if os.name == "posix":
            if info.st_uid != os.getuid():
                raise PermissionError("Private files must be owned by their user")
            os.fchmod(descriptor, 0o600)
            validate_private_acl(descriptor, directory=False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def history_lock(directory: Path, name: str, timeout: float = 10):
    private_directory(directory)
    descriptor = private_open(directory / (name + ".lock"), os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
        else:
            import fcntl

        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Conversation storage is busy") from None
                time.sleep(0.01)
            except OSError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def atomic_history(path: Path, history: list[dict]) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        descriptor = private_open(path, os.O_RDONLY)
        os.close(descriptor)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".history-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(history, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
