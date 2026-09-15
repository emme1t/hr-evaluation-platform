import pytest

from apps.audit.models import AuditLog
from apps.audit.services import AuditContractError
from apps.notifications.models import NotificationAttempt
from apps.notifications.services import (
    NotificationStateError,
    _deliver_attempt,
    _prepare_attempt,
    notify_project_evaluator,
    resend_project_notification,
)


class AcceptedProvider:
    def __init__(self):
        self.calls = 0

    def send_task_summary(self, **kwargs):
        self.calls += 1


class FailedProvider(AcceptedProvider):
    def send_task_summary(self, **kwargs):
        super().send_task_summary(**kwargs)
        error = RuntimeError("fictional provider failure")
        error.code = "FICTIONAL_PROVIDER_FAILURE"
        raise error


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("provider_type", "outcome_action"),
    [
        (AcceptedProvider, "NOTIFICATION_SENT"),
        (FailedProvider, "NOTIFICATION_FAILED"),
    ],
)
def test_outcome_audit_failure_preserves_uncertain_reservation_and_prevents_resend(
    active_project, hr_admin, monkeypatch, provider_type, outcome_action
):
    from apps.notifications import services

    evaluator = active_project.tasks.order_by("pk").first().evaluator
    attempt = _prepare_attempt(active_project, evaluator, "wecom")
    provider = provider_type()
    original_record = services.record_audit

    def fail_outcome(actor, action, target, changes, **kwargs):
        if action == outcome_action:
            raise AuditContractError("forced fictional outcome audit failure")
        return original_record(actor, action, target, changes, **kwargs)

    monkeypatch.setattr(services, "record_audit", fail_outcome)

    with pytest.raises(AuditContractError):
        _deliver_attempt(attempt.pk, provider, hr_admin)

    attempt.refresh_from_db()
    assert provider.calls == 1
    assert attempt.status == NotificationAttempt.Status.QUEUED
    assert attempt.failure_code == "DELIVERY_IN_PROGRESS"
    assert not AuditLog.objects.filter(action=outcome_action).exists()

    with pytest.raises(NotificationStateError) as raised:
        _deliver_attempt(attempt.pk, provider, hr_admin)
    assert raised.value.code == "NOTIFICATION_DELIVERY_UNCERTAIN"
    assert provider.calls == 1


@pytest.mark.django_db(transaction=True)
def test_retry_intent_and_reservation_exist_before_provider_call(
    active_project, hr_admin
):
    evaluator = active_project.tasks.order_by("pk").first().evaluator
    failed = notify_project_evaluator(
        active_project, evaluator, "wecom", FailedProvider()
    )
    observations = []

    class ObservingProvider:
        def send_task_summary(self, **kwargs):
            current = NotificationAttempt.objects.get(
                public_id=kwargs["attempt_public_id"]
            )
            observations.append(
                (
                    current.status,
                    current.failure_code,
                    AuditLog.objects.filter(
                        action="NOTIFICATION_RETRIED",
                        target_id=str(current.public_id),
                    ).count(),
                )
            )

    delivered = resend_project_notification(
        active_project,
        evaluator,
        "wecom",
        hr_admin,
        notifier=ObservingProvider(),
        expected_attempt=str(failed.public_id),
    )

    assert observations == [("queued", "DELIVERY_IN_PROGRESS", 1)]
    assert delivered.status == "sent"
