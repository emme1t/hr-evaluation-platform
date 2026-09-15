import os
from pathlib import Path
import subprocess
import sys

import pytest
from django.conf import settings

from apps.core.tests.offline_test_utils import create_directory_link


@pytest.fixture
def offline_paths(settings, tmp_path):
    root = tmp_path / "fictional-offline-data"
    settings.APP_ENV = "offline"
    settings.OFFLINE_DATA_ROOT = root
    settings.REPORTING_PRIVATE_ROOT = root / "reporting"

    from apps.core.offline.paths import OfflinePaths

    return OfflinePaths.from_settings()


def test_bootstrap_creates_only_private_data_layout(offline_paths):
    offline_paths.ensure_layout()

    assert offline_paths.database.parent.name == "database"
    assert offline_paths.secret.parent.name == "secrets"
    assert offline_paths.logs.is_dir() and offline_paths.backups.is_dir()
    assert offline_paths.database != settings.BASE_DIR / "db.sqlite3"
    assert {item.name for item in offline_paths.root.iterdir()} == {
        "backups",
        "database",
        "exports",
        "locks",
        "logs",
        "reporting",
        "secrets",
        "staging",
        "state",
    }


def test_bootstrap_command_generates_secret_once_without_printing_it(tmp_path):
    data_root = tmp_path / "fictional-offline-data"
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.offline",
        "HR_EVAL_DATA_ROOT": str(data_root),
        "HR_EVAL_PORT": "8765",
    }
    command = [sys.executable, "manage.py", "offline_bootstrap"]

    first = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    secret = data_root / "secrets" / "django-secret.key"
    first_secret = secret.read_text(encoding="utf-8")
    second = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(first_secret) >= 64
    assert secret.read_text(encoding="utf-8") == first_secret
    assert first_secret not in first.stdout + first.stderr + second.stdout + second.stderr
    assert "OFFLINE_BOOTSTRAP_OK" in second.stdout


def test_preflight_rejects_reporting_symlink_outside_private_root(offline_paths, tmp_path):
    offline_paths.ensure_layout()
    outside = tmp_path / "fictional-outside"
    outside.mkdir()
    reporting = offline_paths.reporting
    reporting.rmdir()
    create_directory_link(reporting, outside)

    from apps.core.offline.health import preflight_readiness

    result = preflight_readiness(offline_paths)

    assert result.status == "not_ready"
    assert result.code == "REPORTING_ROOT_INVALID"
