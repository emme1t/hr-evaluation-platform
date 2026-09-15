from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sqlite3
from uuid import uuid4

from .health import _sqlite_failure_code, preflight_readiness
from .paths import OfflinePathError, OfflinePaths


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    code: str


def _checkpoint_database(database_path) -> None:
    with sqlite3.connect(database_path) as database:
        checkpoint = database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not checkpoint or checkpoint[0] != 0:
            raise sqlite3.OperationalError("wal checkpoint busy")


def _lock_owner_is_live(paths: OfflinePaths) -> bool:
    if not paths.lock.exists():
        return False
    try:
        owner = json.loads(paths.lock.read_text(encoding="utf-8"))
        process_id = owner.get("pid")
        if not isinstance(process_id, int) or process_id <= 0:
            return False
        os.kill(process_id, 0)
    except (OSError, ValueError, json.JSONDecodeError, OfflinePathError):
        return False
    return True


def _record_failure(paths: OfflinePaths, code: str) -> RecoveryResult:
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        paths.recovery_diagnostic.write_text(
            json.dumps({"code": code}), encoding="utf-8"
        )
    except (OSError, OfflinePathError):
        pass
    return RecoveryResult("not_recovered", code)


def _write_clean_marker(paths: OfflinePaths) -> bool:
    try:
        paths.clean_marker.write_text(
            json.dumps({"generation": str(uuid4())}), encoding="utf-8"
        )
    except (OSError, OfflinePathError):
        return False
    return True


def recover_dirty_start(paths: OfflinePaths) -> RecoveryResult:
    try:
        dirty = paths.dirty_marker.exists() or not paths.clean_marker.exists()
        if not dirty:
            return RecoveryResult("clean", "OFFLINE_CLEAN")
        if _lock_owner_is_live(paths):
            return _record_failure(paths, "LOCK_OWNER_ACTIVE")
        _checkpoint_database(paths.database)
    except sqlite3.Error as error:
        return _record_failure(paths, _sqlite_failure_code(error))
    except (OSError, OfflinePathError):
        return _record_failure(paths, "DATABASE_UNREADABLE")

    readiness = preflight_readiness(paths)
    if readiness.status != "ready":
        return _record_failure(paths, readiness.code)
    if not _write_clean_marker(paths):
        return _record_failure(paths, "STATE_WRITE_FAILED")
    try:
        paths.dirty_marker.unlink(missing_ok=True)
    except (OSError, OfflinePathError):
        return _record_failure(paths, "STATE_WRITE_FAILED")
    return RecoveryResult("recovered", "OFFLINE_RECOVERY_OK")
