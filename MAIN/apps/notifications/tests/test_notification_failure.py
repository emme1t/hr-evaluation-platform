from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, local

import pytest
from django.db import IntegrityError, close_old_connections, connection
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.evaluations.models import EvaluationProject
from apps.evaluations.services.projects import close_project_early
from apps.notifications.models import NotificationAttempt, NotificationOutbox
from apps.notifications.services import (
    InvalidNotificationChannel,
    NotificationStateError,
    _deliver_attempt,
    _prepare_attempt,
    notify_project_evaluator,
    resend_project_notification,
)
from tests.factories import create_employee, create_project, create_task


@pytest.mark.django_db
def test_failed_attempt_is_retained_when_hr_retries_successfully(
    active_project, evaluator, failing_wecom, fake_wecom, hr_admin
):
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", notifier=failing_wecom
    )
    retried = resend_project_notification(
        active_project,
        evaluator,
        "wecom",
        hr_admin,
        notifier=fake_wecom,
        expected_attempt=str(failed.public_id),
    )

    attempts = list(NotificationAttempt.objects.order_by("attempt"))
    assert failed.status == "failed"
    assert failed.failure_code == "WECOM_TIMEOUT"
    assert retried.status == "sent"
    assert [(row.attempt, row.status, row.failure_code) for row in attempts] == [
        (1, "failed", "WECOM_TIMEOUT"),
        (2, "sent", ""),
    ]
    assert set(attempts[0].tasks.values_list("pk", flat=True)) == set(
        attempts[1].tasks.values_list("pk", flat=True)
    )
    assert attempts[0].project_id == attempts[1].project_id == active_project.pk
    assert attempts[0].recipient_id == attempts[1].recipient_id == evaluator.pk


@pytest.mark.django_db
def test_switching_to_email_appends_attempt_without_overwriting_wecom_failure(
    active_project, evaluator, failing_wecom, hr_admin, mailoutbox
):
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", notifier=failing_wecom
    )
    resend_project_notification(
        active_project,
        evaluator,
        "email",
        hr_admin,
        expected_attempt=str(failed.public_id),
    )

    assert list(NotificationAttempt.objects.values_list("channel", "status")) == [
        ("wecom", "failed"),
        ("email", "sent"),
    ]
    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_invalid_channel_creates_no_outbox_or_attempt(active_project, evaluator):
    with pytest.raises(InvalidNotificationChannel):
        notify_project_evaluator(active_project, evaluator, "sms")
    assert NotificationOutbox.objects.count() == 0
    assert NotificationAttempt.objects.count() == 0


@pytest.mark.django_db
def test_expired_active_project_cannot_create_a_new_notification(
    active_project, evaluator, fake_wecom
):
    active_project.deadline = timezone.now()
    active_project.save(update_fields=["deadline"])
    with pytest.raises(NotificationStateError) as exc_info:
        notify_project_evaluator(active_project, evaluator, "wecom", fake_wecom)
    assert exc_info.value.code == "PROJECT_EXPIRED"
    assert NotificationOutbox.objects.count() == 0
    assert NotificationAttempt.objects.count() == 0


@pytest.mark.django_db
def test_resend_service_has_explicit_first_send_path_when_outbox_is_missing(
    active_project, evaluator, fake_wecom, hr_admin
):
    result = resend_project_notification(
        active_project, evaluator, "wecom", hr_admin, notifier=fake_wecom
    )
    assert result.status == "sent"
    assert result.attempt == 1
    assert NotificationOutbox.objects.count() == 1


@pytest.mark.django_db
def test_outboxes_are_isolated_by_project_and_recipient(
    active_project, evaluator, fake_wecom, hr_admin
):
    category = evaluator.category
    other_evaluator = create_employee(
        "NOTICE-OTHER-EVALUATOR",
        "其他虚构评价人",
        category=category,
        wecom_userid="wx-other-evaluator",
    )
    subject = create_employee(
        "NOTICE-OTHER-SUBJECT", "其他虚构被评价人", category=category
    )
    other_project = create_project(name="其他虚构项目", status="active")
    create_task(
        project=other_project,
        evaluator=other_evaluator,
        subject=subject,
        relationship_type="manager",
    )

    first = notify_project_evaluator(active_project, evaluator, "wecom", fake_wecom)
    second = notify_project_evaluator(
        other_project, other_evaluator, "wecom", fake_wecom
    )

    assert first.outbox_id != second.outbox_id
    assert NotificationOutbox.objects.count() == 2
    assert all(row.task_count > 0 for row in NotificationOutbox.objects.all())


@pytest.mark.django_db
def test_retry_rejects_sent_outbox_and_closed_project(
    active_project, evaluator, fake_wecom, failing_wecom, hr_admin
):
    sent = notify_project_evaluator(active_project, evaluator, "wecom", fake_wecom)
    with pytest.raises(NotificationStateError) as sent_error:
        resend_project_notification(
            active_project,
            evaluator,
            "wecom",
            hr_admin,
            notifier=fake_wecom,
            expected_attempt=str(sent.public_id),
        )
    assert sent_error.value.code == "NOTIFICATION_NOT_RETRYABLE"

    NotificationAttempt.objects.all().delete()
    NotificationOutbox.objects.all().delete()
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    active_project.status = "closed"
    active_project.save(update_fields=["status"])
    with pytest.raises(NotificationStateError) as closed_error:
        resend_project_notification(
            active_project,
            evaluator,
            "wecom",
            hr_admin,
            notifier=fake_wecom,
            expected_attempt=str(failed.public_id),
        )
    assert closed_error.value.code == "PROJECT_NOT_ACTIVE"


@pytest.mark.django_db
def test_same_channel_retry_delivers_existing_queued_attempt_without_duplicate(
    active_project, evaluator, failing_wecom, fake_wecom, hr_admin
):
    attempt = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    NotificationAttempt.objects.filter(pk=attempt.pk).update(
        status="queued", failure_code="", completed_at=None
    )
    NotificationOutbox.objects.filter(pk=attempt.outbox_id).update(status="queued")

    result = resend_project_notification(
        active_project,
        evaluator,
        "wecom",
        hr_admin,
        notifier=fake_wecom,
        expected_attempt=str(attempt.public_id),
    )

    assert result.pk == attempt.pk
    assert result.status == "sent"
    assert NotificationAttempt.objects.count() == 1
    assert len(fake_wecom.calls) == 1


@pytest.mark.django_db
def test_channel_switch_supersedes_queued_attempt_and_appends_email_history(
    active_project, evaluator, failing_wecom, hr_admin, mailoutbox
):
    attempt = notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    NotificationAttempt.objects.filter(pk=attempt.pk).update(
        status="queued", failure_code="", completed_at=None
    )
    NotificationOutbox.objects.filter(pk=attempt.outbox_id).update(status="queued")

    result = resend_project_notification(
        active_project,
        evaluator,
        "email",
        hr_admin,
        expected_attempt=str(attempt.public_id),
    )

    assert result.channel == "email"
    assert result.status == "sent"
    assert list(
        NotificationAttempt.objects.values_list(
            "attempt", "channel", "status", "failure_code"
        )
    ) == [
        (1, "wecom", "failed", "NOTIFICATION_SUPERSEDED"),
        (2, "email", "sent", ""),
    ]
    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_outbox_unique_race_uses_savepoint_and_converges_after_integrity_error(
    active_project, evaluator, failing_wecom, fake_wecom, monkeypatch
):
    notify_project_evaluator(
        active_project, evaluator, "wecom", failing_wecom
    )
    initial_get = True
    original_get = QuerySet.get
    original_create = NotificationOutbox.objects.create

    def miss_once(queryset, *args, **kwargs):
        nonlocal initial_get
        if queryset.model is NotificationOutbox and initial_get:
            initial_get = False
            raise NotificationOutbox.DoesNotExist
        return original_get(queryset, *args, **kwargs)

    def collide_with_existing_row(**kwargs):
        return original_create(**kwargs)

    monkeypatch.setattr(QuerySet, "get", miss_once)
    monkeypatch.setattr(NotificationOutbox.objects, "create", collide_with_existing_row)

    result = notify_project_evaluator(
        active_project, evaluator, "wecom", fake_wecom
    )

    assert result.status == "sent"
    assert NotificationOutbox.objects.count() == 1
    assert result.attempt == 2


@pytest.mark.django_db
def test_stale_queued_delivery_cannot_overwrite_latest_outbox_status(
    active_project, evaluator, fake_wecom, mailoutbox
):
    stale_wecom = _prepare_attempt(active_project, evaluator, "wecom")
    latest_email = _prepare_attempt(active_project, evaluator, "email")

    latest_result = _deliver_attempt(latest_email.pk)
    stale_result = _deliver_attempt(stale_wecom.pk, fake_wecom)

    stale_result.refresh_from_db()
    latest_result.refresh_from_db()
    latest_result.outbox.refresh_from_db()
    assert latest_result.status == NotificationAttempt.Status.SENT
    assert stale_result.status == NotificationAttempt.Status.FAILED
    assert stale_result.failure_code == "NOTIFICATION_SUPERSEDED"
    assert latest_result.outbox.status == NotificationOutbox.Status.SENT
    assert fake_wecom.calls == []
    assert len(mailoutbox) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("invalid_state", "expected_code"),
    [("closed", "PROJECT_NOT_ACTIVE"), ("expired", "PROJECT_EXPIRED")],
)
def test_queued_delivery_rechecks_project_state_before_external_call(
    active_project,
    evaluator,
    fake_wecom,
    invalid_state,
    expected_code,
):
    attempt = _prepare_attempt(active_project, evaluator, "wecom")
    if invalid_state == "closed":
        EvaluationProject.objects.filter(pk=active_project.pk).update(status="closed")
    else:
        EvaluationProject.objects.filter(pk=active_project.pk).update(
            deadline=timezone.now()
        )

    result = _deliver_attempt(attempt.pk, fake_wecom)

    result.refresh_from_db()
    result.outbox.refresh_from_db()
    assert fake_wecom.calls == []
    assert result.status == NotificationAttempt.Status.FAILED
    assert result.failure_code == expected_code
    assert result.outbox.status == NotificationOutbox.Status.FAILED


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="project close versus delivery row-lock semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_postgresql_close_commits_before_blocked_delivery_and_prevents_send(
    active_project, evaluator, fake_wecom, hr_admin, monkeypatch
):
    attempt = _prepare_attempt(active_project, evaluator, "wecom")
    close_project_locked = Event()
    release_close = Event()
    delivery_project_lock_attempted = Event()
    delivery_project_locked = Event()
    worker_role = local()
    observed_roles = set()
    observation_guard = Lock()
    original_get = QuerySet.get

    def observe_real_project_lock(queryset, *args, **kwargs):
        role = getattr(worker_role, "value", None)
        is_first_lock_for_role = False
        if queryset.model is EvaluationProject and queryset.query.select_for_update:
            with observation_guard:
                if role in {"close", "deliver"} and role not in observed_roles:
                    observed_roles.add(role)
                    is_first_lock_for_role = True
            if is_first_lock_for_role and role == "deliver":
                delivery_project_lock_attempted.set()
        row = original_get(queryset, *args, **kwargs)
        if is_first_lock_for_role and role == "close":
            close_project_locked.set()
            assert release_close.wait(timeout=10)
        elif is_first_lock_for_role and role == "deliver":
            delivery_project_locked.set()
        return row

    monkeypatch.setattr(QuerySet, "get", observe_real_project_lock)
    project_id = active_project.pk
    actor_id = hr_admin.pk

    def close_in_own_connection():
        close_old_connections()
        worker_role.value = "close"
        try:
            project = EvaluationProject.objects.get(pk=project_id)
            actor = type(hr_admin).objects.get(pk=actor_id)
            close_project_early(project, actor)
            return "closed"
        finally:
            close_old_connections()

    def deliver_in_own_connection():
        close_old_connections()
        worker_role.value = "deliver"
        try:
            return _deliver_attempt(attempt.pk, fake_wecom).failure_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_future = pool.submit(close_in_own_connection)
        assert close_project_locked.wait(timeout=10)
        delivery_future = pool.submit(deliver_in_own_connection)
        assert delivery_project_lock_attempted.wait(timeout=10)
        assert not delivery_project_locked.wait(timeout=0.5)
        assert fake_wecom.calls == []
        release_close.set()
        assert close_future.result(timeout=20) == "closed"
        assert delivery_future.result(timeout=20) == "PROJECT_NOT_ACTIVE"

    attempt.refresh_from_db()
    active_project.refresh_from_db()
    assert active_project.status == EvaluationProject.Status.CLOSED
    assert attempt.status == NotificationAttempt.Status.FAILED
    assert fake_wecom.calls == []


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update delivery concurrency requires PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_postgresql_second_queued_delivery_blocks_on_project_lock_and_sends_once(
    active_project, evaluator, fake_wecom, monkeypatch
):
    attempt = _prepare_attempt(active_project, evaluator, "wecom")
    first_project_locked = Event()
    release_first = Event()
    second_lock_attempted = Event()
    second_project_locked = Event()
    worker_role = local()
    observed_roles = set()
    observation_guard = Lock()
    original_get = QuerySet.get

    def observe_real_project_lock(queryset, *args, **kwargs):
        role = getattr(worker_role, "value", None)
        is_first_lock_for_role = False
        if queryset.model is EvaluationProject and queryset.query.select_for_update:
            with observation_guard:
                if role in {"first", "second"} and role not in observed_roles:
                    observed_roles.add(role)
                    is_first_lock_for_role = True
            if is_first_lock_for_role and role == "second":
                second_lock_attempted.set()
        row = original_get(queryset, *args, **kwargs)
        if is_first_lock_for_role and role == "first":
            first_project_locked.set()
            assert release_first.wait(timeout=10)
        elif is_first_lock_for_role and role == "second":
            second_project_locked.set()
        return row

    monkeypatch.setattr(QuerySet, "get", observe_real_project_lock)

    def deliver_in_own_connection(role):
        close_old_connections()
        worker_role.value = role
        try:
            return _deliver_attempt(attempt.pk, fake_wecom).status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deliver_in_own_connection, "first")
        assert first_project_locked.wait(timeout=10)
        second = pool.submit(deliver_in_own_connection, "second")
        assert second_lock_attempted.wait(timeout=10)
        assert not second_project_locked.wait(timeout=0.5)
        assert fake_wecom.calls == []
        release_first.set()
        assert first.result(timeout=20) == "sent"
        assert second.result(timeout=20) == "sent"

    attempt.refresh_from_db()
    attempt.outbox.refresh_from_db()
    assert len(fake_wecom.calls) == 1
    assert NotificationAttempt.objects.filter(outbox=attempt.outbox).count() == 1
    assert attempt.status == NotificationAttempt.Status.SENT
    assert attempt.outbox.status == NotificationOutbox.Status.SENT


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update channel-switch concurrency requires PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_email_switch_appends_and_sends_one_attempt(
    active_project, evaluator, hr_admin, monkeypatch
):
    queued = _prepare_attempt(active_project, evaluator, "wecom")
    first_project_locked = Event()
    release_first = Event()
    second_lock_attempted = Event()
    second_project_locked = Event()
    worker_role = local()
    observed_roles = set()
    observation_guard = Lock()
    send_calls = []
    send_guard = Lock()
    original_get = QuerySet.get

    def observe_real_project_lock(queryset, *args, **kwargs):
        role = getattr(worker_role, "value", None)
        is_first_lock_for_role = False
        if queryset.model is EvaluationProject and queryset.query.select_for_update:
            with observation_guard:
                if role in {"first", "second"} and role not in observed_roles:
                    observed_roles.add(role)
                    is_first_lock_for_role = True
            if is_first_lock_for_role and role == "second":
                second_lock_attempted.set()
        row = original_get(queryset, *args, **kwargs)
        if is_first_lock_for_role and role == "first":
            first_project_locked.set()
            assert release_first.wait(timeout=10)
        elif is_first_lock_for_role and role == "second":
            second_project_locked.set()
        return row

    def record_email_send(**kwargs):
        with send_guard:
            send_calls.append(kwargs["attempt_public_id"])

    monkeypatch.setattr(QuerySet, "get", observe_real_project_lock)
    monkeypatch.setattr(
        "apps.notifications.services.send_task_summary_email", record_email_send
    )
    project_id = active_project.pk
    evaluator_id = evaluator.pk
    actor_id = hr_admin.pk

    def switch_in_own_connection(role):
        close_old_connections()
        worker_role.value = role
        try:
            project = type(active_project).objects.get(pk=project_id)
            recipient = type(evaluator).objects.get(pk=evaluator_id)
            actor = type(hr_admin).objects.get(pk=actor_id)
            try:
                result = resend_project_notification(
                    project,
                    recipient,
                    "email",
                    actor,
                    expected_attempt=str(queued.public_id),
                )
            except NotificationStateError as exc:
                return exc.code
            return result.status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(switch_in_own_connection, "first")
        assert first_project_locked.wait(timeout=10)
        second = pool.submit(switch_in_own_connection, "second")
        assert second_lock_attempted.wait(timeout=10)
        assert not second_project_locked.wait(timeout=0.5)
        assert send_calls == []
        release_first.set()
        results = [first.result(timeout=20), second.result(timeout=20)]

    queued.refresh_from_db()
    queued.outbox.refresh_from_db()
    attempts = list(queued.outbox.attempts.order_by("attempt"))
    assert sorted(results) == ["NOTIFICATION_NOT_RETRYABLE", "sent"]
    assert send_calls == [str(attempts[1].public_id)]
    assert [row.attempt for row in attempts] == [1, 2]
    assert [row.channel for row in attempts] == ["wecom", "email"]
    assert [row.status for row in attempts] == ["failed", "sent"]
    assert attempts[0].failure_code == "NOTIFICATION_SUPERSEDED"
    assert queued.outbox.status == NotificationOutbox.Status.SENT
