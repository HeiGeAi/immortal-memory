"""Serialize Feishu collectors sharing local state."""
from contextlib import contextmanager
import fcntl


@contextmanager
def collector_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
