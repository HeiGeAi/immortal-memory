"""Transactional publication and rollback for one SQLite file generation."""

import os
from pathlib import Path
import stat as stat_module
from typing import Callable, Dict, Optional, Tuple
import uuid


class RecoveryError(RuntimeError):
    """The pre-call database generation could not be restored automatically."""


Fsync = Callable[[Path], None]
RollbackArtifacts = Dict[Path, Optional[Path]]


def generation_paths(database: Path) -> Tuple[Path, Path, Path]:
    return (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
    )


def create_rollback_artifacts(
    database: Path,
    fsync_directory: Fsync,
) -> RollbackArtifacts:
    token = f"{os.getpid()}.{uuid.uuid4().hex}"
    artifacts: RollbackArtifacts = {}
    try:
        for position, original in enumerate(generation_paths(database)):
            if not original.exists():
                artifacts[original] = None
                continue
            if original.is_symlink() or not stat_module.S_ISREG(
                original.lstat().st_mode
            ):
                raise ValueError(
                    f"unsafe database generation artifact: {original}"
                )
            backup = database.parent / (
                f"{database.name}.rollback.{token}.{position}"
            )
            os.link(str(original), str(backup))
            artifacts[original] = backup
        fsync_directory(database.parent)
        return artifacts
    except Exception:
        for backup in artifacts.values():
            if backup is not None and backup.exists():
                backup.unlink()
        raise


def restore_rollback_artifacts(
    database: Path,
    artifacts: RollbackArtifacts,
    fsync_file: Fsync,
    fsync_directory: Fsync,
) -> None:
    restore_temps = []
    try:
        for position, (original, backup) in enumerate(artifacts.items()):
            if backup is None:
                if original.exists() or original.is_symlink():
                    original.unlink()
                continue
            restore_temp = database.parent / (
                f"{database.name}.rollback.restore.{os.getpid()}.{position}"
            )
            if restore_temp.exists():
                restore_temp.unlink()
            os.link(str(backup), str(restore_temp))
            restore_temps.append(restore_temp)
            os.replace(str(restore_temp), str(original))
            fsync_file(original)
        fsync_directory(database.parent)
    except Exception as exc:
        for restore_temp in restore_temps:
            if restore_temp.exists():
                restore_temp.unlink()
        raise RecoveryError(
            "database generation recovery failed; rollback artifacts retained"
        ) from exc


def cleanup_rollback_artifacts(
    database: Path,
    artifacts: RollbackArtifacts,
    fsync_directory: Fsync,
) -> None:
    try:
        for backup in artifacts.values():
            if backup is not None and backup.exists():
                backup.unlink()
        fsync_directory(database.parent)
    except Exception as exc:
        raise RecoveryError(
            "rollback artifact cleanup failed; manual inspection required"
        ) from exc


def replace_database(
    staging: Path,
    database: Path,
    fsync_file: Fsync,
    fsync_directory: Fsync,
) -> None:
    # Staging is a complete DELETE-journal snapshot. The generation lock
    # prevents readers from opening main/WAL/SHM while this unit is switched.
    #
    # 先把旧 -wal/-shm rename 成带 token 的废弃名，再 replace 主库：
    # 保证任何崩溃点都不会出现「新主库配旧 WAL」（SQLite 打开时会按 WAL
    # salt 把旧帧回放进新主库，造成页级错乱）。replace 失败则把旧 WAL/SHM
    # 放回原位，保持旧库一致可用。
    token = f"{os.getpid()}.{uuid.uuid4().hex}"
    quarantined = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            stale = Path(str(sidecar) + f".stale-{token}")
            os.replace(str(sidecar), str(stale))
            quarantined.append((sidecar, stale))
    try:
        os.replace(str(staging), str(database))
    except Exception:
        for sidecar, stale in quarantined:
            try:
                os.replace(str(stale), str(sidecar))
            except OSError:
                pass
        raise
    for _sidecar, stale in quarantined:
        try:
            stale.unlink()
        except OSError:
            pass
    fsync_file(database)
    fsync_directory(database.parent)
