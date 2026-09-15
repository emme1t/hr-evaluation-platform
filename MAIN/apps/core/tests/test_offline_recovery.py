import json
import os
import sqlite3
import subprocess
import sys

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader


pytestmark = pytest.mark.django_db


@pytest.fixture
def offline_paths(settings, tmp_path):
    root = tmp_path / "fictional-offline-data"
    settings.APP_ENV = "offline"
    settings.OFFLINE_DATA_ROOT = root
    settings.REPORTING_PRIVATE_ROOT = root / "reporting"

    from apps.core.offline.paths import OfflinePaths

    paths = OfflinePaths.from_settings()
    paths.ensure_layout()
    paths.ensure_secret()
    return paths


def _ready_database(paths):
    leaves = MigrationLoader(connection).graph.leaf_nodes()
    with sqlite3.connect(paths.database) as database:
        database.execute(
            "CREATE TABLE django_migrations (id integer primary key, app varchar(255), name varchar(255), applied datetime)"
        )
        database.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, CURRENT_TIMESTAMP)",
            leaves,
        )


def test_dirty_marker_runs_integrity_then_clears_only_after_success(offline_paths):
    _ready_database(offline_paths)
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "recovered"
    assert result.code == "OFFLINE_RECOVERY_OK"
    assert not offline_paths.dirty_marker.exists()
    assert json.loads(offline_paths.clean_marker.read_text(encoding="utf-8"))["generation"]


def test_dirty_start_refuses_busy_wal_checkpoint(offline_paths):
    _ready_database(offline_paths)
    writer = sqlite3.connect(offline_paths.database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE fictional_wal_probe (value text)")
    writer.execute("INSERT INTO fictional_wal_probe VALUES ('fictional')")
    writer.commit()
    writer.execute("BEGIN")
    writer.execute("SELECT value FROM fictional_wal_probe").fetchone()
    assert offline_paths.database.with_name(offline_paths.database.name + "-wal").exists()
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)
    writer.close()

    assert result.status == "not_recovered"
    assert result.code == "DATABASE_CHECKPOINT_BUSY"
    assert offline_paths.dirty_marker.exists()


def test_dirty_start_replays_crash_left_wal_before_marking_clean(offline_paths):
    _ready_database(offline_paths)
    probe = """
import os
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
database.execute("PRAGMA journal_mode=WAL")
database.execute("PRAGMA wal_autocheckpoint=0")
database.execute("CREATE TABLE fictional_wal_probe (value text)")
database.execute("INSERT INTO fictional_wal_probe VALUES ('fictional')")
database.commit()
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", probe, os.fspath(offline_paths.database)],
        check=True,
        timeout=10,
    )
    wal = offline_paths.database.with_name(offline_paths.database.name + "-wal")
    assert wal.exists()
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "recovered"
    assert result.code == "OFFLINE_RECOVERY_OK"
    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(offline_paths.database) as database:
        assert database.execute("SELECT value FROM fictional_wal_probe").fetchone() == (
            "fictional",
        )


def test_dirty_start_preserves_marker_for_corrupt_database(offline_paths):
    offline_paths.database.write_bytes(b"not a sqlite database")
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == "DATABASE_INTEGRITY_FAILED"
    assert offline_paths.dirty_marker.exists()
    assert json.loads(offline_paths.recovery_diagnostic.read_text(encoding="utf-8")) == {
        "code": "DATABASE_INTEGRITY_FAILED"
    }


def test_dirty_start_preserves_marker_for_foreign_key_failure(offline_paths):
    _ready_database(offline_paths)
    with sqlite3.connect(offline_paths.database) as database:
        database.execute("CREATE TABLE fictional_parent (id integer primary key)")
        database.execute(
            "CREATE TABLE fictional_child (parent_id integer references fictional_parent(id))"
        )
        database.execute("INSERT INTO fictional_child VALUES (999)")
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == "DATABASE_FOREIGN_KEY_FAILED"
    assert offline_paths.dirty_marker.exists()


def test_dirty_start_preserves_marker_for_missing_migration_leaf(offline_paths):
    with sqlite3.connect(offline_paths.database) as database:
        database.execute(
            "CREATE TABLE django_migrations (id integer primary key, app varchar(255), name varchar(255), applied datetime)"
        )
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == "MIGRATION_DRIFT"
    assert offline_paths.dirty_marker.exists()


def test_dirty_start_preserves_marker_for_invalid_secret(offline_paths):
    _ready_database(offline_paths)
    offline_paths.secret.write_text("", encoding="utf-8")
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == "SECRET_INVALID"
    assert offline_paths.dirty_marker.exists()


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ("attempt to write a readonly database", "DATABASE_READ_ONLY"),
        ("database or disk is full", "DATABASE_FULL"),
    ],
)
def test_dirty_start_returns_redacted_checkpoint_write_failures(
    offline_paths, monkeypatch, message, expected_code
):
    _ready_database(offline_paths)
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")

    from apps.core.offline import recovery

    def fail_checkpoint(_database):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(recovery, "_checkpoint_database", fail_checkpoint)
    result = recovery.recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == expected_code
    assert offline_paths.dirty_marker.exists()


def test_dirty_start_refuses_a_stated_live_lock_owner(offline_paths):
    _ready_database(offline_paths)
    offline_paths.dirty_marker.write_text("starting", encoding="utf-8")
    offline_paths.lock.write_text(json.dumps({"pid": __import__("os").getpid()}))

    from apps.core.offline.recovery import recover_dirty_start

    result = recover_dirty_start(offline_paths)

    assert result.status == "not_recovered"
    assert result.code == "LOCK_OWNER_ACTIVE"
    assert offline_paths.dirty_marker.exists()
