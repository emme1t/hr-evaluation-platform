import json
import sqlite3

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader


@pytest.mark.django_db
def test_health_reports_application_and_database(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_offline_health_returns_redacted_503_until_readiness_succeeds(
    client, settings, tmp_path
):
    settings.APP_ENV = "offline"
    settings.OFFLINE_DATA_ROOT = tmp_path / "fictional-offline-data"
    settings.REPORTING_PRIVATE_ROOT = settings.OFFLINE_DATA_ROOT / "reporting"

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "OFFLINE_NOT_READY",
        "mode": "offline",
    }
    assert str(settings.OFFLINE_DATA_ROOT) not in response.content.decode()


@pytest.mark.django_db
def test_offline_health_reports_generation_only_after_real_readiness(
    client, settings, tmp_path
):
    settings.APP_ENV = "offline"
    settings.OFFLINE_DATA_ROOT = tmp_path / "fictional-offline-data"
    settings.REPORTING_PRIVATE_ROOT = settings.OFFLINE_DATA_ROOT / "reporting"
    from apps.core.offline.paths import OfflinePaths

    paths = OfflinePaths.from_settings()
    paths.ensure_layout()
    paths.ensure_secret()
    leaves = MigrationLoader(connection).graph.leaf_nodes()
    with sqlite3.connect(paths.database) as database:
        database.execute(
            "CREATE TABLE django_migrations (id integer primary key, app varchar(255), name varchar(255), applied datetime)"
        )
        database.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, CURRENT_TIMESTAMP)",
            leaves,
        )
    paths.clean_marker.write_text(
        json.dumps({"generation": "fictional-generation"}), encoding="utf-8"
    )

    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "mode": "offline",
        "generation": "fictional-generation",
    }
