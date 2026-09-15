import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.notifications.services import validated_public_base_url


def test_disabled_outbound_backend_fails_closed():
    from apps.notifications.offline import DisabledOutboundBackend

    with pytest.raises(ImproperlyConfigured, match="禁止发送"):
        DisabledOutboundBackend().send_messages([])


def test_offline_verify_accepts_the_task12_audit_contract():
    from django.core.management import call_command

    call_command("offline_verify", verbosity=0)


def test_offline_sender_reaches_disabled_backend_without_network_or_attribute_error(
    tmp_path,
):
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.offline",
        "HR_EVAL_DATA_ROOT": str(tmp_path / "fictional-offline-data"),
        "HR_EVAL_PORT": "8765",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json\n"
                "import socket\n"
                "from django.conf import settings\n"
                "from django.core.exceptions import ImproperlyConfigured\n"
                "from apps.notifications.email import send_task_summary_email\n"
                "attempts = []\n"
                "def reject_network(*args, **kwargs):\n"
                "    attempts.append((args, kwargs))\n"
                "    raise AssertionError('network must not be used')\n"
                "socket.create_connection = reject_network\n"
                "try:\n"
                "    send_task_summary_email(recipient_email='fictional@example.test', "
                "subject='fictional subject', body='fictional body')\n"
                "except ImproperlyConfigured as exc:\n"
                "    print(json.dumps({'message': str(exc), "
                "'network_attempts': len(attempts), "
                "'email_timeout': settings.EMAIL_TIMEOUT, "
                "'smtp': [settings.EMAIL_HOST, settings.EMAIL_PORT, "
                "settings.EMAIL_USE_TLS, settings.EMAIL_HOST_USER, "
                "settings.EMAIL_HOST_PASSWORD, settings.DEFAULT_FROM_EMAIL]}))\n"
                "else:\n"
                "    raise AssertionError('disabled backend did not fail closed')\n"
            ),
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert "禁止发送" in outcome["message"]
    assert outcome["network_attempts"] == 0
    assert outcome["email_timeout"] > 0
    assert outcome["smtp"] == ["", 0, False, "", "", ""]


def _production_settings_process(**overrides):
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "SECRET_KEY": "fake-production-test-secret",
        "ALLOWED_HOSTS": "evaluation.hr.internal",
        "POSTGRES_DB": "fake_db",
        "POSTGRES_USER": "fake_user",
        "POSTGRES_PASSWORD": "fake_password",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "EVALUATION_PUBLIC_BASE_URL": "https://evaluation.hr.internal",
        "REPORTING_PRIVATE_ROOT": str(
            Path(tempfile.gettempdir()) / "hr-evaluation-reporting-test"
        ),
        "EMAIL_TIMEOUT": "10",
    }
    environment.update(overrides)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from django.conf import settings; "
                "print(settings.EVALUATION_PUBLIC_BASE_URL); "
                "print(settings.EMAIL_TIMEOUT)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_production_settings_require_explicit_valid_public_base_url():
    valid = _production_settings_process()
    assert valid.returncode == 0
    assert "https://evaluation.hr.internal" in valid.stdout

    missing = _production_settings_process(EVALUATION_PUBLIC_BASE_URL="")
    assert missing.returncode != 0
    assert "EVALUATION_PUBLIC_BASE_URL" in missing.stderr


def test_production_settings_require_explicit_reporting_private_root():
    missing = _production_settings_process(REPORTING_PRIVATE_ROOT="")
    assert missing.returncode != 0
    assert "REPORTING_PRIVATE_ROOT" in missing.stderr


@pytest.mark.parametrize(
    ("url", "allowed_hosts"),
    [
        ("http://evaluation.hr.internal", "evaluation.hr.internal"),
        ("https://user@evaluation.hr.internal", "evaluation.hr.internal"),
        ("https://evaluation.hr.internal?token=fake", "evaluation.hr.internal"),
        ("https://evaluation.hr.internal/#fragment", "evaluation.hr.internal"),
        ("https://other.hr.internal", "evaluation.hr.internal"),
        ("https://evaluation.example.test", "evaluation.example.test"),
    ],
)
def test_production_settings_reject_unsafe_public_base_url(url, allowed_hosts):
    result = _production_settings_process(
        EVALUATION_PUBLIC_BASE_URL=url,
        ALLOWED_HOSTS=allowed_hosts,
    )
    assert result.returncode != 0
    assert "EVALUATION_PUBLIC_BASE_URL" in result.stderr


@pytest.mark.parametrize(
    "value", ["", "0", "-1", "nan", "inf", "not-a-number"]
)
def test_production_settings_reject_invalid_email_timeout(value):
    result = _production_settings_process(EMAIL_TIMEOUT=value)
    assert result.returncode != 0
    assert "EMAIL_TIMEOUT" in result.stderr


def test_local_public_url_is_loopback_http_and_send_validation_is_environment_aware(
    settings,
):
    settings.APP_ENV = "local"
    settings.EVALUATION_PUBLIC_BASE_URL = "http://127.0.0.1:8000"
    settings.ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    assert validated_public_base_url() == "http://127.0.0.1:8000"

    settings.EVALUATION_PUBLIC_BASE_URL = "https://evaluation.hr.internal"
    with pytest.raises(ImproperlyConfigured):
        validated_public_base_url()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://evaluation.hr.internal",
        "https://user:pass@evaluation.hr.internal",
        "https://evaluation.hr.internal?query=value",
        "https://evaluation.hr.internal/#fragment",
        "https://evaluation.example.test",
        "https://other.hr.internal",
    ],
)
def test_runtime_production_url_validation_fails_closed(settings, url):
    settings.APP_ENV = "production"
    settings.ALLOWED_HOSTS = ["evaluation.hr.internal"]
    settings.EVALUATION_PUBLIC_BASE_URL = url
    with pytest.raises(ImproperlyConfigured):
        validated_public_base_url()
