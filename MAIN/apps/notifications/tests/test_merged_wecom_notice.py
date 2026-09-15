from datetime import timedelta

import pytest
from django.utils import timezone

from apps.notifications.models import NotificationAttempt, NotificationOutbox
from apps.notifications.services import (
    _deliver_attempt,
    _prepare_attempt,
    launch_project,
    notify_project_evaluator,
)
from tests.factories import create_employee, create_task


@pytest.mark.django_db
def test_one_wecom_message_summarizes_all_unfinished_project_tasks(
    active_project, fake_wecom, evaluator
):
    result = notify_project_evaluator(
        active_project, evaluator, "wecom", notifier=fake_wecom
    )

    assert result.status == NotificationAttempt.Status.SENT
    assert result.task_count == 3
    assert len(fake_wecom.calls) == 1
    message = fake_wecom.calls[0]
    assert message["userid"] == evaluator.wecom_userid
    assert message["title"] == "您有新的评价任务"
    assert f"您好，{evaluator.name}" in message["description"]
    assert active_project.name in message["description"]
    assert "您有 3 项待评价任务" in message["description"]
    assert timezone.localtime(active_project.deadline).strftime("%Y-%m-%d %H:%M") in message[
        "description"
    ]
    assert message["url"] == (
        f"https://evaluation.example.test/projects/{active_project.public_id}/tasks/"
    )
    assert evaluator.name not in message["url"]
    assert evaluator.employee_no not in message["url"]
    assert evaluator.corporate_email not in message["url"]
    assert message["attempt_public_id"] == str(result.public_id)


@pytest.mark.django_db
def test_wecom_delivery_uses_attempt_payload_snapshot_after_current_data_changes(
    active_project, fake_wecom, evaluator
):
    original_name = evaluator.name
    original_project_name = active_project.name
    original_deadline = timezone.localtime(active_project.deadline).strftime(
        "%Y-%m-%d %H:%M"
    )
    attempt = _prepare_attempt(active_project, evaluator, "wecom")

    evaluator.name = "快照后修改的评价人"
    evaluator.save(update_fields=["name"])
    active_project.name = "快照后修改的项目"
    active_project.deadline += timedelta(days=5)
    active_project.save(update_fields=["name", "deadline"])

    result = _deliver_attempt(attempt.pk, fake_wecom)

    attempt.refresh_from_db()
    assert result.status == NotificationAttempt.Status.SENT
    assert attempt.payload_snapshot["channel"] == "wecom"
    assert attempt.payload_snapshot["title"] == "您有新的评价任务"
    assert original_name in fake_wecom.calls[0]["description"]
    assert original_project_name in fake_wecom.calls[0]["description"]
    assert original_deadline in fake_wecom.calls[0]["description"]
    assert evaluator.name not in fake_wecom.calls[0]["description"]
    assert active_project.name not in fake_wecom.calls[0]["description"]


@pytest.mark.django_db(transaction=True)
def test_launch_transitions_ready_project_once_after_commit(
    project_with_three_tasks, fake_wecom, evaluator, hr_admin
):
    first = launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)
    second = launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)

    project_with_three_tasks.refresh_from_db()
    assert project_with_three_tasks.status == "active"
    assert project_with_three_tasks.launched_at is not None
    assert first.sent_recipient_count == 1
    assert first.already_launched is False
    assert second.already_launched is True
    assert len(fake_wecom.calls) == 1
    assert NotificationOutbox.objects.filter(project=project_with_three_tasks).count() == 1
    assert NotificationAttempt.objects.filter(
        outbox__project=project_with_three_tasks
    ).count() == 1


@pytest.mark.django_db
def test_launch_does_not_send_before_outer_transaction_commits(
    project_with_three_tasks, fake_wecom, hr_admin, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)
        assert fake_wecom.calls == []
    assert len(callbacks) == 1


@pytest.mark.django_db(transaction=True)
def test_launch_keeps_project_active_when_one_recipient_delivery_fails(
    project_with_three_tasks, failing_wecom, hr_admin
):
    result = launch_project(project_with_three_tasks, hr_admin, notifier=failing_wecom)

    project_with_three_tasks.refresh_from_db()
    attempt = NotificationAttempt.objects.get(outbox__project=project_with_three_tasks)
    assert result.sent_recipient_count == 1
    assert project_with_three_tasks.status == "active"
    assert attempt.status == "failed"
    assert attempt.failure_code == "WECOM_TIMEOUT"


@pytest.mark.django_db(transaction=True)
def test_launch_continues_after_first_recipient_fails_and_records_each_result(
    project_with_three_tasks, evaluator, hr_admin
):
    second_evaluator = create_employee(
        "NOTICE-EVALUATOR-SECOND",
        "第二位虚构评价人",
        category=evaluator.category,
        wecom_userid="wx-notice-evaluator-second",
    )
    subject = project_with_three_tasks.tasks.order_by("pk").first().subject
    create_task(
        project=project_with_three_tasks,
        evaluator=second_evaluator,
        subject=subject,
        relationship_type="manager",
    )

    class FailFirstNotifier:
        def __init__(self):
            self.calls = []

        def send_task_summary(self, **payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                error = RuntimeError("fictional first-recipient failure")
                error.code = "WECOM_TIMEOUT"
                raise error

    notifier = FailFirstNotifier()

    result = launch_project(project_with_three_tasks, hr_admin, notifier=notifier)

    attempts = list(
        NotificationAttempt.objects.filter(project=project_with_three_tasks).order_by(
            "recipient_id"
        )
    )
    assert result.sent_recipient_count == 2
    assert len(notifier.calls) == 2
    assert [(row.status, row.failure_code) for row in attempts] == [
        ("failed", "WECOM_TIMEOUT"),
        ("sent", ""),
    ]
    assert [row.task_count for row in attempts] == [3, 1]


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["draft", "closed", "archived"])
def test_launch_rejects_stale_or_illegal_project_states(
    project_with_three_tasks, fake_wecom, hr_admin, status
):
    from apps.notifications.services import NotificationStateError

    project_with_three_tasks.status = status
    project_with_three_tasks.save(update_fields=["status"])
    with pytest.raises(NotificationStateError) as exc_info:
        launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)
    assert exc_info.value.code == "PROJECT_NOT_READY"
    assert fake_wecom.calls == []
    assert NotificationOutbox.objects.count() == 0
