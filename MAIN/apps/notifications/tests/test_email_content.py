import json
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.notifications.models import EmailMagicLink, NotificationAttempt
from apps.notifications.services import _deliver_attempt, _prepare_attempt, notify_project_evaluator


@pytest.mark.django_db
def test_email_fallback_uses_utf8_dynamic_names_and_no_attachment(
    active_project, evaluator, mailoutbox
):
    result = notify_project_evaluator(active_project, evaluator, "email")

    assert result.status == "sent"
    assert len(mailoutbox) == 1
    message = mailoutbox[0]
    assert message.to == [evaluator.corporate_email]
    assert message.subject == "恩力公司人事部门人事评价项目"
    assert message.body.startswith(f"您好，{evaluator.name}：")
    for name in active_project.tasks.filter(evaluator=evaluator).values_list(
        "subject__name", flat=True
    ):
        assert name in message.body
    assert active_project.name in message.body
    assert "??" not in message.body
    assert message.attachments == []
    assert "https://evaluation.example.test/auth/email/confirm/#" in message.body
    assert evaluator.employee_no not in message.body
    assert evaluator.corporate_email not in message.body
    token = re.search(r"/auth/email/confirm/#([^\s]+)", message.body).group(1)
    assert not EmailMagicLink.objects.filter(token_hash=token).exists()


@pytest.mark.django_db
def test_email_failure_records_stable_code_without_token_or_backend_response(
    active_project, evaluator, settings, mailoutbox
):
    evaluator.corporate_email = ""
    evaluator.save(update_fields=["corporate_email"])

    result = notify_project_evaluator(active_project, evaluator, "email")

    assert result.status == "failed"
    assert result.failure_code == "EMAIL_ADDRESS_MISSING"
    assert result.failure_code.isascii()
    assert result.failure_code == result.failure_code.upper()
    assert mailoutbox == []
    assert EmailMagicLink.objects.count() == 0


@pytest.mark.django_db
def test_default_test_wecom_adapter_fails_closed_without_network(
    active_project, evaluator, monkeypatch
):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("network must not be reached in tests")

    monkeypatch.setattr("httpx.Client.post", forbidden_network)
    result = notify_project_evaluator(active_project, evaluator, "wecom")
    assert result.status == "failed"
    assert result.failure_code == "WECOM_DISABLED"


@pytest.mark.django_db
def test_email_uses_frozen_subject_name_instead_of_changed_roster_name(
    active_project, evaluator, mailoutbox
):
    task = active_project.tasks.filter(evaluator=evaluator).order_by("pk").first()
    task.project_subject.subject_snapshot = {
        "public_id": str(task.subject.public_id),
        "name": "冻结被评价人姓名",
        "department_level_1": task.subject.department_level_1,
        "department_level_2": task.subject.department_level_2,
    }
    task.project_subject.save(update_fields=["subject_snapshot"])
    task.subject.name = "启动后修改的当前姓名"
    task.subject.save(update_fields=["name"])

    notify_project_evaluator(active_project, evaluator, "email")

    assert "冻结被评价人姓名" in mailoutbox[0].body
    assert "启动后修改的当前姓名" not in mailoutbox[0].body


@pytest.mark.django_db
def test_unconfigured_production_email_fails_closed_before_network(
    active_project, evaluator, settings, monkeypatch
):
    settings.APP_ENV = "production"
    settings.ALLOWED_HOSTS = ["evaluation.hr.internal"]
    settings.EVALUATION_PUBLIC_BASE_URL = "https://evaluation.hr.internal"
    settings.EMAIL_HOST = ""
    settings.DEFAULT_FROM_EMAIL = ""

    def forbidden_send(*args, **kwargs):
        raise AssertionError("SMTP must not be reached without configuration")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", forbidden_send)
    result = notify_project_evaluator(active_project, evaluator, "email")
    assert result.status == "failed"
    assert result.failure_code == "EMAIL_NOT_CONFIGURED"


@pytest.mark.django_db
def test_email_backend_receives_finite_timeout_and_timeout_is_stable_failure(
    active_project, evaluator, settings, monkeypatch
):
    settings.APP_ENV = "production"
    settings.ALLOWED_HOSTS = ["evaluation.hr.internal"]
    settings.EVALUATION_PUBLIC_BASE_URL = "https://evaluation.hr.internal"
    settings.EMAIL_HOST = "smtp.hr.internal"
    settings.DEFAULT_FROM_EMAIL = "hr@example.test"
    settings.EMAIL_TIMEOUT = 2.5
    captured = {}

    class TimeoutConnection:
        def send_messages(self, messages):
            raise TimeoutError("fictional SMTP timeout")

    def connection_factory(*, timeout):
        captured["timeout"] = timeout
        return TimeoutConnection()

    monkeypatch.setattr(
        "apps.notifications.email.get_connection", connection_factory, raising=False
    )
    result = notify_project_evaluator(active_project, evaluator, "email")

    assert captured == {"timeout": 2.5}
    assert result.status == "failed"
    assert result.failure_code == "EMAIL_TIMEOUT"
    persisted = NotificationAttempt.objects.get(pk=result.pk)
    assert persisted.status == NotificationAttempt.Status.FAILED
    assert persisted.failure_code == "EMAIL_TIMEOUT"


@pytest.mark.django_db
def test_email_deadline_uses_local_timezone(active_project, evaluator, mailoutbox):
    notify_project_evaluator(active_project, evaluator, "email")
    expected = timezone.localtime(active_project.deadline).strftime("%Y-%m-%d %H:%M")
    assert f"截止时间：{expected}" in mailoutbox[0].body


@pytest.mark.django_db
def test_email_delivery_uses_frozen_safe_snapshot_without_persisting_raw_token(
    active_project, evaluator, mailoutbox
):
    original_name = evaluator.name
    original_project_name = active_project.name
    original_deadline = timezone.localtime(active_project.deadline).strftime(
        "%Y-%m-%d %H:%M"
    )
    attempt = _prepare_attempt(active_project, evaluator, "email")

    evaluator.name = "邮件快照后修改的评价人"
    evaluator.save(update_fields=["name"])
    active_project.name = "邮件快照后修改的项目"
    active_project.deadline += timedelta(days=5)
    active_project.save(update_fields=["name", "deadline"])

    result = _deliver_attempt(attempt.pk)

    attempt.refresh_from_db()
    raw_token = re.search(
        r"/auth/email/confirm/#([^\s]+)", mailoutbox[0].body
    ).group(1)
    serialized_snapshot = json.dumps(attempt.payload_snapshot, ensure_ascii=False)
    assert result.status == NotificationAttempt.Status.SENT
    assert attempt.payload_snapshot["channel"] == "email"
    assert attempt.payload_snapshot["attachments"] == []
    assert "[一次性链接]" in attempt.payload_snapshot["body"]
    assert raw_token not in serialized_snapshot
    assert original_name in mailoutbox[0].body
    assert original_project_name in mailoutbox[0].body
    assert original_deadline in mailoutbox[0].body
    assert evaluator.name not in mailoutbox[0].body
    assert active_project.name not in mailoutbox[0].body
