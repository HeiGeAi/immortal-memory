"""Cross-process maintenance gate for authoritative fact writers."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class MaintenanceInProgress(RuntimeError):
    pass


def _open_directory_fd(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = list(absolute.parts)
    note_positions = [
        index for index, part in enumerate(parts) if part == "notes"
    ]
    if note_positions:
        boundary = note_positions[-1]
        vault_anchor = Path(*parts[:boundary])
        if vault_anchor.exists():
            anchor = vault_anchor.resolve(strict=True)
            remaining = parts[boundary:]
        else:
            anchor = vault_anchor.parent.resolve(strict=True)
            remaining = [vault_anchor.name, *parts[boundary:]]
    else:
        anchor = Path(absolute.anchor)
        remaining = parts[1:]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(anchor, flags)
    try:
        for part in remaining:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _publication_pending(directory_fd: int) -> bool:
    try:
        metadata = os.stat(
            "publication.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MaintenanceInProgress("notes_migration_metadata_invalid")
    return True


@contextmanager
def writer_access(vault: Path) -> Iterator[None]:
    directory_fd = _open_directory_fd(
        Path(vault) / "notes" / "migration",
        create=True,
    )
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open("migration.lock", flags, 0o600, dir_fd=directory_fd)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceInProgress("notes_migration_in_progress") from exc
        if _publication_pending(directory_fd):
            raise MaintenanceInProgress("notes_migration_in_progress")
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(directory_fd)
