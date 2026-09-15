from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from django.db import connection
from django.db.migrations.loader import MigrationLoader

from .paths import OfflinePathError, OfflinePaths


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    code: str


def _sqlite_failure_code(error: sqlite3.Error) -> str:
    message = str(error).lower()
    if "checkpoint busy" in message:
        return "DATABASE_CHECKPOINT_BUSY"
    if "readonly" in message:
        return "DATABASE_READ_ONLY"
    if "disk is full" in message or "database or disk is full" in message:
        return "DATABASE_FULL"
    if "file is not a database" in message or "malformed" in message:
        return "DATABASE_INTEGRITY_FAILED"
    return "DATABASE_UNREADABLE"


def _migration_leaves() -> set[tuple[str, str]]:
    return set(MigrationLoader(connection).graph.leaf_nodes())


def _sqlite_regexp(pattern, value) -> bool:
    return bool(re.search(pattern, value or ""))


def preflight_readiness(paths: OfflinePaths) -> ReadinessResult:
    try:
        reporting = paths.reporting
        database_path = paths.database
        secret_path = paths.secret
        if not reporting.is_dir():
            return ReadinessResult("not_ready", "REPORTING_ROOT_INVALID")
        if not database_path.is_file():
            return ReadinessResult("not_ready", "DATABASE_MISSING")
        if not secret_path.is_file():
            return ReadinessResult("not_ready", "SECRET_INVALID")
        try:
            if len(secret_path.read_text(encoding="utf-8").strip()) < 64:
                return ReadinessResult("not_ready", "SECRET_INVALID")
        except (OSError, UnicodeError):
            return ReadinessResult("not_ready", "SECRET_INVALID")
    except OfflinePathError:
        return ReadinessResult("not_ready", "REPORTING_ROOT_INVALID")

    try:
        with sqlite3.connect(database_path) as database:
            # Django creates SQLite constraints that use this connection-local
            # function.  Register it before integrity_check opens those tables.
            database.create_function("REGEXP", 2, _sqlite_regexp)
            integrity = database.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                return ReadinessResult("not_ready", "DATABASE_INTEGRITY_FAILED")
            if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return ReadinessResult("not_ready", "DATABASE_FOREIGN_KEY_FAILED")
            try:
                applied = set(
                    database.execute("SELECT app, name FROM django_migrations").fetchall()
                )
            except sqlite3.Error:
                return ReadinessResult("not_ready", "MIGRATION_DRIFT")
    except sqlite3.Error as error:
        return ReadinessResult("not_ready", _sqlite_failure_code(error))

    if not _migration_leaves().issubset(applied):
        return ReadinessResult("not_ready", "MIGRATION_DRIFT")
    return ReadinessResult("ready", "OFFLINE_READY")
