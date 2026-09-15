import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

from apps.core.tests.offline_test_utils import create_directory_link
from config.settings.validation import (
    validate_offline_data_root,
    validate_offline_port,
)


APP_BASE = Path(__file__).resolve().parents[3]


def _offline_settings_process(data_root):
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.offline",
        "HR_EVAL_DATA_ROOT": str(data_root),
        "HR_EVAL_PORT": "8765",
        "WECOM_CORP_ID": "fictional-corp-id",
        "WECOM_AGENT_ID": "fictional-agent-id",
        "WECOM_SECRET": "fictional-secret",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from django.conf import settings; "
                "from apps.reporting.storage import private_root; "
                "print(json.dumps({'app_env': settings.APP_ENV, "
                "'allowed_hosts': settings.ALLOWED_HOSTS, "
                "'public_base_url': settings.EVALUATION_PUBLIC_BASE_URL, "
                "'database_engine': settings.DATABASES['default']['ENGINE'], "
                "'database_name': str(settings.DATABASES['default']['NAME']), "
                "'email_backend': settings.EMAIL_BACKEND, "
                "'wecom': [settings.WECOM_CORP_ID, settings.WECOM_AGENT_ID, "
                "settings.WECOM_SECRET], "
                "'reporting_private_root': str(private_root())}))"
            ),
        ],
        cwd=APP_BASE,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _offline_connection_process(data_root):
    (data_root / "database").mkdir(parents=True)
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.offline",
        "HR_EVAL_DATA_ROOT": str(data_root),
        "HR_EVAL_PORT": "8765",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; import django; django.setup(); "
                "from django.db import connection; "
                "cursor = connection.cursor(); "
                "print(json.dumps({"
                "'foreign_keys': cursor.execute('PRAGMA foreign_keys').fetchone()[0], "
                "'journal_mode': cursor.execute('PRAGMA journal_mode').fetchone()[0], "
                "'busy_timeout': cursor.execute('PRAGMA busy_timeout').fetchone()[0], "
                "'synchronous': cursor.execute('PRAGMA synchronous').fetchone()[0]}))"
            ),
        ],
        cwd=APP_BASE,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_offline_settings_are_loopback_sqlite_and_outbound_free(tmp_path):
    data_root = tmp_path / "fictional-offline-data"
    result = _offline_settings_process(data_root)

    assert result.returncode == 0, result.stderr
    configured = json.loads(result.stdout)
    assert configured["app_env"] == "offline"
    assert configured["allowed_hosts"] == ["127.0.0.1", "localhost"]
    assert configured["public_base_url"] == "http://127.0.0.1:8765"
    assert configured["database_engine"] == "django.db.backends.sqlite3"
    assert configured["database_name"] == str(
        data_root / "database" / "hr_evaluation.sqlite3"
    )
    assert configured["email_backend"] == "apps.notifications.offline.DisabledOutboundBackend"
    assert configured["wecom"] == ["", "", ""]
    assert configured["reporting_private_root"] == str(data_root / "reporting")


def test_offline_sqlite_connection_enforces_durability_pragmas(tmp_path):
    result = _offline_connection_process(tmp_path / "fictional-offline-data")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 5000,
        "synchronous": 1,
    }


def test_offline_root_rejects_application_tree_and_invalid_port(tmp_path):
    with pytest.raises(ImproperlyConfigured, match="数据目录不能位于应用目录"):
        validate_offline_data_root(str(APP_BASE / "data"), app_base=APP_BASE)
    with pytest.raises(ImproperlyConfigured, match="数据目录不能包含应用目录"):
        validate_offline_data_root(str(APP_BASE.parent), app_base=APP_BASE)
    assert validate_offline_data_root(
        str(tmp_path / "fictional-offline-data"), app_base=APP_BASE
    ) == (tmp_path / "fictional-offline-data").resolve()
    assert validate_offline_port("8765") == 8765
    with pytest.raises(ImproperlyConfigured, match="端口"):
        validate_offline_port("0")


def test_offline_root_resolves_windows_capable_symlink_containment_both_directions(
    tmp_path,
):
    application = tmp_path / "fictional-application"
    application.mkdir()
    alias_to_application = tmp_path / "fictional-alias-to-application"
    create_directory_link(alias_to_application, application)

    with pytest.raises(ImproperlyConfigured, match="数据目录不能位于应用目录"):
        validate_offline_data_root(
            str(alias_to_application / "data"), app_base=application
        )
    with pytest.raises(ImproperlyConfigured, match="数据目录不能包含应用目录"):
        validate_offline_data_root(
            str(tmp_path), app_base=alias_to_application
        )


def test_offline_verify_fails_closed_when_task12_audit_service_cannot_import(
    monkeypatch,
):
    from apps.core.management.commands.offline_verify import Command

    original_import = importlib.import_module

    def reject_task12_audit_service(name, package=None):
        if name == "apps.audit.services":
            raise ImportError("fictional audit service import failure")
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", reject_task12_audit_service)

    with pytest.raises(CommandError, match="OFFLINE_AUDIT_UNAVAILABLE"):
        Command().handle()
