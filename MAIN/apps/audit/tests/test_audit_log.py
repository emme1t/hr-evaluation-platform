import json

import pytest
from django.contrib.auth.models import Group

from apps.audit.models import AuditLog, AuditLogImmutableError
from apps.audit.services import (
    AUDIT_ACTION_RULES,
    AuditContractError,
    record_audit,
    validate_audit_contract,
)
from tests.factories import create_employee


EXPECTED_ACTION_RULES = {
    "AUTH_LOGIN_SUCCEEDED": (("HR_ADMIN", "HR_OPERATOR", "EVALUATOR"), "accounts.user"),
    "AUTH_ACCESS_DENIED": (("HR_ADMIN", "HR_OPERATOR", "EVALUATOR"), "accounts.user"),
    "EMPLOYEE_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employee"),
    "EMPLOYEE_UPDATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employee"),
    "EMPLOYEE_DEACTIVATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employee"),
    "ROSTER_IMPORT_COMMITTED": (("HR_ADMIN", "HR_OPERATOR"), "roster.importbatch"),
    "CATEGORY_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employeecategory"),
    "CATEGORY_UPDATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employeecategory"),
    "CATEGORY_DEACTIVATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.employeecategory"),
    "RELATIONSHIP_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.evaluationrelationship"),
    "RELATIONSHIP_UPDATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.evaluationrelationship"),
    "RELATIONSHIP_DEACTIVATED": (("HR_ADMIN", "HR_OPERATOR"), "roster.evaluationrelationship"),
    "RELATIONSHIP_IMPORT_COMMITTED": (("HR_ADMIN", "HR_OPERATOR"), "roster.importbatch"),
    "TEMPLATE_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.formtemplate"),
    "TEMPLATE_VERSION_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.formtemplate"),
    "TEMPLATE_SEALED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.formtemplate"),
    "PROJECT_CREATED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.evaluationproject"),
    "PROJECT_PREPARED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.evaluationproject"),
    "PROJECT_LAUNCHED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.evaluationproject"),
    "PROJECT_DEADLINE_EXTENDED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.evaluationproject"),
    "PROJECT_CLOSED": (("HR_ADMIN", "HR_OPERATOR"), "evaluations.evaluationproject"),
    "EVALUATION_SUBMITTED": (("EVALUATOR",), "evaluations.evaluationtask"),
    "NOTIFICATION_SENT": (("HR_ADMIN", "HR_OPERATOR"), "notifications.notificationattempt"),
    "NOTIFICATION_FAILED": (("HR_ADMIN", "HR_OPERATOR"), "notifications.notificationattempt"),
    "NOTIFICATION_RETRIED": (("HR_ADMIN", "HR_OPERATOR"), "notifications.notificationattempt"),
    "REPORT_SUMMARY_EXPORTED": (("HR_ADMIN",), "evaluations.evaluationproject"),
    "REPORT_RAW_EXPORTED": (("HR_ADMIN",), "evaluations.evaluationproject"),
    "REPORT_EXCEPTION_EXPORTED": (("HR_ADMIN",), "evaluations.evaluationproject"),
    "PROJECT_RECOMPUTED": (("HR_ADMIN",), "evaluations.evaluationproject"),
    "AUDIT_EXPORTED": (("HR_ADMIN",), "accounts.user"),
}


def test_inferred_recovery_vocabulary_is_exactly_30_validated_rows():
    actual = {
        action: (rule.allowed_roles, rule.target_type)
        for action, rule in AUDIT_ACTION_RULES.items()
    }

    assert actual == EXPECTED_ACTION_RULES
    assert len(actual) == 30


@pytest.mark.parametrize(
    ("action", "allowed_roles", "target_type"),
    [
        (action, allowed_roles, target_type)
        for action, (allowed_roles, target_type) in EXPECTED_ACTION_RULES.items()
    ],
)
def test_every_inferred_action_accepts_only_its_role_and_target_contract(
    action, allowed_roles, target_type
):
    for role in allowed_roles:
        validate_audit_contract(action, role, target_type)

    wrong_role = next(
        role
        for role in ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR")
        if role not in allowed_roles
    ) if len(allowed_roles) < 3 else "UNKNOWN_ROLE"
    with pytest.raises(AuditContractError):
        validate_audit_contract(action, wrong_role, target_type)
    with pytest.raises(AuditContractError):
        validate_audit_contract(action, allowed_roles[0], "wrong.target")


@pytest.mark.django_db
def test_audit_log_recursively_redacts_secrets_scores_answers_and_urls(hr_admin):
    employee = create_employee("AUDIT-001", "Fictional Audit Employee")

    event = record_audit(
        hr_admin,
        "EMPLOYEE_UPDATED",
        employee,
        {
            "safe": "department changed",
            "nested": [
                {"smtp_password": "fictional-secret-value"},
                {"raw_score": 5, "answer_text": "private answer"},
                {"callback_url": "https://example.test/auth/email/confirm/#fictional"},
            ],
        },
        idempotency_key="audit-redaction-1",
        correlation_id="request-redaction-1",
    )

    serialized = json.dumps(event.change_summary, ensure_ascii=False)
    assert "fictional-secret-value" not in serialized
    assert "private answer" not in serialized
    assert "https://" not in serialized
    assert event.change_summary["safe"] == "department changed"
    assert event.change_summary["nested"] == [
        {"smtp_password": "[REDACTED]"},
        {"raw_score": "[REDACTED]", "answer_text": "[REDACTED]"},
        {"callback_url": "[REDACTED]"},
    ]


@pytest.mark.django_db
def test_audit_log_reloads_actor_role_and_rejects_stale_or_wrong_target(hr_admin):
    employee = create_employee("AUDIT-002", "Fictional Stale Role Employee")
    hr_admin.groups.clear()
    hr_admin.groups.add(Group.objects.get(name="EVALUATOR"))

    with pytest.raises(AuditContractError):
        record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, {})
    with pytest.raises(AuditContractError):
        record_audit(hr_admin, "EVALUATION_SUBMITTED", employee, {})


@pytest.mark.django_db
@pytest.mark.parametrize("repeats", [2, 5, 10])
def test_idempotency_key_collapses_repeated_audit_append(hr_admin, repeats):
    employee = create_employee(
        f"AUDIT-REPEAT-{repeats}", f"Fictional Repeat Employee {repeats}"
    )

    events = [
        record_audit(
            hr_admin,
            "EMPLOYEE_UPDATED",
            employee,
            {"field": "department"},
            idempotency_key=f"repeat-{repeats}",
        )
        for _ in range(repeats)
    ]

    assert len({event.pk for event in events}) == 1
    assert AuditLog.objects.filter(idempotency_key=f"repeat-{repeats}").count() == 1


@pytest.mark.django_db
def test_reused_idempotency_key_with_different_payload_fails_closed(hr_admin):
    employee = create_employee("AUDIT-003", "Fictional Conflict Employee")
    record_audit(
        hr_admin,
        "EMPLOYEE_UPDATED",
        employee,
        {"field": "name"},
        idempotency_key="conflict-key",
    )

    with pytest.raises(AuditContractError):
        record_audit(
            hr_admin,
            "EMPLOYEE_UPDATED",
            employee,
            {"field": "department"},
            idempotency_key="conflict-key",
        )


@pytest.mark.django_db
def test_oversized_deep_or_malformed_summary_fails_closed(hr_admin):
    employee = create_employee("AUDIT-004", "Fictional Bounds Employee")
    too_deep = {"value": 1}
    for _ in range(9):
        too_deep = {"nested": too_deep}

    with pytest.raises(AuditContractError):
        record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, {"safe": "x" * 9000})
    with pytest.raises(AuditContractError):
        record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, too_deep)
    with pytest.raises(AuditContractError):
        record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, {1: "non-string-key"})


@pytest.mark.django_db
def test_saved_audit_rows_cannot_be_changed_deleted_or_bulk_mutated(hr_admin):
    employee = create_employee("AUDIT-005", "Fictional Immutable Employee")
    event = record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, {})

    event.change_summary = {"tampered": True}
    with pytest.raises(AuditLogImmutableError):
        event.save()
    with pytest.raises(AuditLogImmutableError):
        event.delete()
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.filter(pk=event.pk).update(action="EMPLOYEE_CREATED")
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.filter(pk=event.pk).delete()
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.bulk_update([event], ["change_summary"])
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.bulk_create(
            [event], update_conflicts=True, update_fields=["change_summary"], unique_fields=["id"]
        )


@pytest.mark.django_db
def test_base_manager_and_plain_bulk_create_cannot_bypass_append_contract(hr_admin):
    employee = create_employee("AUDIT-BASE", "Fictional Base Manager Employee")
    update_event = record_audit(
        hr_admin, "EMPLOYEE_UPDATED", employee, {}, idempotency_key="base-update"
    )
    delete_event = record_audit(
        hr_admin, "EMPLOYEE_UPDATED", employee, {}, idempotency_key="base-delete"
    )

    with pytest.raises(AuditLogImmutableError):
        AuditLog._base_manager.filter(pk=update_event.pk).update(
            action="EMPLOYEE_CREATED"
        )
    with pytest.raises(AuditLogImmutableError):
        AuditLog._base_manager.filter(pk=delete_event.pk).delete()
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.bulk_create(
            [
                AuditLog(
                    actor=hr_admin,
                    effective_role="HR_ADMIN",
                    action="EMPLOYEE_UPDATED",
                    target_type="roster.employee",
                    target_id=str(employee.public_id),
                    idempotency_key="plain-bulk-create",
                    correlation_id="plain-bulk-create",
                    change_summary={},
                )
            ]
        )

    assert AuditLog.objects.filter(pk__in=[update_event.pk, delete_event.pk]).count() == 2
