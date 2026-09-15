import json

import pytest

from apps.audit.models import AuditLog
from tests.factories import create_user_with_role


@pytest.mark.django_db
def test_denial_logging_is_minimal_and_rate_bounded(client, user_with_role):
    client.force_login(user_with_role)

    for _ in range(10):
        response = client.get("/hr/audit/?target_id=fictional-sensitive-object")
        assert response.status_code == 403

    events = AuditLog.objects.filter(
        actor=user_with_role, action="AUTH_ACCESS_DENIED"
    )
    assert 1 <= events.count() <= 5
    serialized = json.dumps(
        list(events.values_list("change_summary", flat=True)), ensure_ascii=False
    )
    assert "fictional-sensitive-object" not in serialized
    assert "target_id" not in serialized
    assert all(
        set(summary) <= {"endpoint_group", "reason"}
        for summary in events.values_list("change_summary", flat=True)
    )


@pytest.mark.django_db
def test_operator_denial_never_reaches_admin_audit_filters(client):
    from tests.factories import create_user_with_role

    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.get("/hr/audit/?action=REPORT_RAW_EXPORTED")

    assert response.status_code == 403
    event = AuditLog.objects.get(actor=operator, action="AUTH_ACCESS_DENIED")
    assert event.target_id == str(operator.public_id)
    assert event.change_summary == {"endpoint_group": "hr", "reason": "forbidden"}


@pytest.mark.django_db
def test_non_hr_forbidden_response_does_not_create_hr_denial_audit(client):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.post("/auth/email/confirm/", {"token": "fictional-invalid"})

    assert response.status_code == 403
    assert not AuditLog.objects.filter(
        actor=operator, action="AUTH_ACCESS_DENIED"
    ).exists()
