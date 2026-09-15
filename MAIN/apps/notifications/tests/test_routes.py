from html.parser import HTMLParser
import json
import re
from datetime import timedelta

import pytest
from django.utils import timezone
from django.test import Client
from django.urls import reverse

from apps.notifications.models import NotificationAttempt
from apps.notifications.services import (
    DELIVERY_IN_PROGRESS,
    _prepare_attempt,
    notify_project_evaluator,
)


class _StatusPageDOM(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            self.text_parts.append(stripped)


def _parse_status_page(response):
    dom = _StatusPageDOM()
    dom.feed(response.content.decode("utf-8"))
    return dom


@pytest.mark.django_db
def test_notification_status_requires_hr_role(
    active_project, evaluator, user_with_role, fake_wecom, client, hr_admin
):
    notify_project_evaluator(active_project, evaluator, "wecom", fake_wecom)
    url = reverse("notifications:project-status", args=[active_project.public_id])

    assert client.get(url).status_code == 302
    client.force_login(user_with_role)
    assert client.get(url).status_code == 403
    client.force_login(hr_admin)
    response = client.get(url)
    assert response.status_code == 200
    assert response.context["outboxes"][0].recipient_id == evaluator.pk


@pytest.mark.django_db
def test_notification_mutations_require_post_hr_and_csrf(
    active_project, evaluator, failing_wecom, hr_admin, user_with_role
):
    attempt = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    retry_url = reverse("notifications:retry", args=[attempt.outbox.public_id])
    email_url = reverse("notifications:switch-email", args=[attempt.outbox.public_id])

    ordinary = Client()
    ordinary.force_login(user_with_role)
    assert ordinary.post(retry_url).status_code == 403
    assert ordinary.post(email_url).status_code == 403

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(hr_admin)
    assert csrf_client.get(retry_url).status_code == 405
    assert csrf_client.get(email_url).status_code == 405
    assert csrf_client.post(retry_url).status_code == 403
    assert csrf_client.post(email_url).status_code == 403


@pytest.mark.django_db
def test_notification_email_switch_accepts_valid_hr_csrf_post(
    active_project, evaluator, failing_wecom, hr_admin
):
    attempt = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    status_url = reverse(
        "notifications:project-status", args=[active_project.public_id]
    )
    email_url = reverse("notifications:switch-email", args=[attempt.outbox.public_id])
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(hr_admin)
    page = csrf_client.get(status_url)

    response = csrf_client.post(
        email_url,
        {
            "csrfmiddlewaretoken": page.cookies["csrftoken"].value,
            "expected_attempt": str(attempt.public_id),
        },
    )

    assert response.status_code == 302
    assert attempt.outbox.attempts.count() == 2


@pytest.mark.django_db
def test_notification_mutation_rejects_missing_and_cross_outbox_control(
    active_project, evaluator, failing_wecom, hr_admin, client
):
    from apps.notifications.services import _prepare_attempt
    from tests.factories import create_employee, create_task

    attempt = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    other = create_employee(
        "NOTICE-CONTROL-OTHER",
        "Fictional Control Other",
        category=evaluator.category,
        wecom_userid="wx-fictional-control-other",
    )
    create_task(
        project=active_project,
        evaluator=other,
        subject=evaluator,
        relationship_type="manager",
    )
    other_attempt = _prepare_attempt(active_project, other, "wecom")
    url = reverse("notifications:retry", args=[attempt.outbox.public_id])
    client.force_login(hr_admin)
    before = NotificationAttempt.objects.count()

    missing = client.post(url, {})
    crossed = client.post(
        url, {"expected_attempt": str(other_attempt.public_id)}
    )

    assert missing.status_code == crossed.status_code == 409
    assert NotificationAttempt.objects.count() == before


@pytest.mark.django_db
def test_launch_route_requires_post_and_hr(active_project, hr_admin, user_with_role):
    active_project.status = "ready"
    active_project.launched_at = None
    active_project.save(update_fields=["status", "launched_at"])
    url = reverse("notifications:launch", args=[active_project.public_id])

    non_hr = Client()
    non_hr.force_login(user_with_role)
    assert non_hr.post(url).status_code == 403

    hr = Client()
    hr.force_login(hr_admin)
    assert hr.get(url).status_code == 405


@pytest.mark.django_db
def test_status_page_renders_complete_attempt_history_and_safe_delivery_previews(
    active_project, evaluator, failing_wecom, hr_admin, client, mailoutbox
):
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    from apps.notifications.services import resend_project_notification

    sent = resend_project_notification(
        active_project,
        evaluator,
        "email",
        hr_admin,
        expected_attempt=str(failed.public_id),
    )
    raw_token = re.search(
        r"/auth/email/confirm/#([^\s]+)", mailoutbox[0].body
    ).group(1)
    original_project_name = active_project.name
    original_recipient_name = evaluator.name
    original_deadline = timezone.localtime(active_project.deadline).strftime(
        "%Y-%m-%d %H:%M"
    )
    active_project.name = "发送后修改的项目名"
    active_project.deadline += timedelta(days=20)
    active_project.save(update_fields=["name", "deadline"])
    evaluator.name = "发送后修改的评价人"
    evaluator.save(update_fields=["name"])
    client.force_login(hr_admin)

    response = client.get(
        reverse("notifications:project-status", args=[active_project.public_id])
    )

    assert response.status_code == 200
    dom = _parse_status_page(response)
    attempt_rows = [
        attrs
        for tag, attrs in dom.elements
        if tag == "tr" and "data-attempt-number" in attrs
    ]
    assert attempt_rows == [
        {
            "data-attempt-number": "1",
            "data-channel": "wecom",
            "data-status": "failed",
        },
        {
            "data-attempt-number": "2",
            "data-channel": "email",
            "data-status": "sent",
        },
    ]
    page_text = " ".join(dom.text_parts)
    assert failed.failure_code in page_text
    assert str(failed.task_count) in page_text
    assert timezone.localtime(failed.created_at).strftime("%Y-%m-%d %H:%M:%S") in page_text
    assert "您有新的评价任务" in page_text
    assert f"您有 {failed.task_count} 项待评价任务" in page_text
    assert f"/projects/{active_project.public_id}/tasks/" in page_text
    assert mailoutbox[0].subject in page_text
    assert original_project_name in page_text
    assert original_recipient_name in page_text
    assert original_deadline in page_text
    preview_text = json.dumps(
        [
            attempt.delivery_preview
            for attempt in response.context["outboxes"][0].attempt_history
        ],
        ensure_ascii=False,
    )
    assert active_project.name not in preview_text
    assert evaluator.name not in preview_text
    assert timezone.localtime(active_project.deadline).strftime(
        "%Y-%m-%d %H:%M"
    ) not in preview_text
    assert "附件：无" in page_text
    assert "[一次性链接]" in page_text
    assert raw_token not in response.content.decode("utf-8")
    assert sent.public_id != failed.public_id


@pytest.mark.django_db
def test_status_page_escapes_employee_and_preview_content(
    active_project, evaluator, failing_wecom, hr_admin, client
):
    evaluator.name = '<img src=x onerror="alert(1)">'
    evaluator.save(update_fields=["name"])
    notify_project_evaluator(active_project, evaluator, "wecom", failing_wecom)
    client.force_login(hr_admin)

    response = client.get(
        reverse("notifications:project-status", args=[active_project.public_id])
    )

    html = response.content.decode("utf-8")
    dom = _parse_status_page(response)
    assert '<img src=x onerror="alert(1)">' not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert all(tag != "img" for tag, _ in dom.elements)


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_state", ["expired", "closed"])
def test_inactive_project_hides_and_rejects_notification_actions(
    active_project,
    evaluator,
    failing_wecom,
    hr_admin,
    client,
    invalid_state,
):
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    if invalid_state == "expired":
        active_project.deadline = timezone.now()
        active_project.save(update_fields=["deadline"])
    else:
        active_project.status = "closed"
        active_project.save(update_fields=["status"])
    retry_url = reverse("notifications:retry", args=[failed.outbox.public_id])
    email_url = reverse("notifications:switch-email", args=[failed.outbox.public_id])
    client.force_login(hr_admin)

    page = client.get(
        reverse("notifications:project-status", args=[active_project.public_id])
    )
    history_count = NotificationAttempt.objects.count()
    retry = client.post(retry_url)
    email = client.post(email_url)

    html = page.content.decode("utf-8")
    assert retry_url not in html
    assert email_url not in html
    assert retry.status_code == 409
    assert email.status_code == 409
    assert NotificationAttempt.objects.count() == history_count


@pytest.mark.django_db
def test_uncertain_delivery_hides_mutations_and_explains_manual_reconciliation(
    active_project, evaluator, hr_admin, client
):
    attempt = _prepare_attempt(active_project, evaluator, "wecom")
    NotificationAttempt.objects.filter(pk=attempt.pk).update(
        failure_code=DELIVERY_IN_PROGRESS
    )
    retry_url = reverse("notifications:retry", args=[attempt.outbox.public_id])
    email_url = reverse(
        "notifications:switch-email", args=[attempt.outbox.public_id]
    )
    reason_id = f"delivery-reconciliation-{attempt.outbox.public_id}"
    client.force_login(hr_admin)

    response = client.get(
        reverse("notifications:project-status", args=[active_project.public_id])
    )

    assert response.status_code == 200
    outbox = response.context["outboxes"][0]
    assert outbox.requires_manual_reconciliation is True
    assert outbox.can_mutate is False
    html = response.content.decode("utf-8")
    assert retry_url not in html
    assert email_url not in html
    dom = _parse_status_page(response)
    assert (
        "p",
        {"id": reason_id, "role": "status"},
    ) in dom.elements
    assert any(
        tag == "td" and attrs.get("aria-describedby") == reason_id
        for tag, attrs in dom.elements
    )
    page_text = " ".join(dom.text_parts)
    assert "发送结果待人工核对" in page_text
    assert "确认服务商侧结果后再决定是否重试" in page_text
