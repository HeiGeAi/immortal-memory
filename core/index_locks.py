"""POSIX advisory locks for the JSONL source and SQLite read model."""

from contextlib import contextmanager
import fcntl
from pathlib import Path


def source_lock_path(source: Path) -> Path:
    return Path(str(Path(source)) + ".source.lock")


def database_lock_path(database: Path) -> Path:
    return Path(str(Path(database)) + ".generation.lock")


@contextmanager
def _path_lock(path: Path, exclusive: bool):
    path = Path(path)
    with path.open("a+b") as handle:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_lock(source: Path, exclusive: bool):
    """Lock the source. Writers use exclusive; readers/reconcilers use shared."""
    return _path_lock(source_lock_path(source), exclusive)


def database_lock(database: Path, exclusive: bool):
    """Lock one published SQLite main/WAL/SHM generation."""
    return _path_lock(database_lock_path(database), exclusive)


@contextmanager
def index_lock_pair(
    source: Path,
    database: Path,
    *,
    source_exclusive: bool,
    database_exclusive: bool,
):
    """Acquire locks in the only supported order: source, then database."""
    with source_lock(source, exclusive=source_exclusive):
        with database_lock(database, exclusive=database_exclusive):
            yield
