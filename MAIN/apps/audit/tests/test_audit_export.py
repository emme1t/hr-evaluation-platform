import pytest
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record_audit
from tests.factories import create_employee, create_user_with_role


@pytest.mark.django_db
def test_audit_export_is_post_admin_only_private_and_audited_after_response(client, hr_admin):
    employee = create_employee("EXPORT-AUDIT", "Fictional Audit Export Employee")
    record_audit(hr_admin, "EMPLOYEE_UPDATED", employee, {"fields": ["name"]})
    client.force_login(hr_admin)

    assert client.get("/hr/audit/export/").status_code == 405
    today = timezone.localdate().isoformat()
    response = client.post("/hr/audit/export/", {"start": today, "end": today})

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert b"EMPLOYEE_UPDATED" in response.content
    assert AuditLog.objects.filter(
        actor=hr_admin,
        action="AUDIT_EXPORTED",
        target_id=str(hr_admin.public_id),
    ).count() == 1

    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)
    assert client.post("/hr/audit/export/").status_code == 403


@pytest.mark.django_db
def test_audit_export_rejects_invalid_window_without_success_event(client, hr_admin):
    client.force_login(hr_admin)

    response = client.post(
        "/hr/audit/export/", {"start": "2026-01-01", "end": "2026-08-27"}
    )

    assert response.status_code == 400
    assert not AuditLog.objects.filter(action="AUDIT_EXPORTED").exists()


@pytest.mark.django_db
def test_audit_export_requires_csrf(hr_admin):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(hr_admin)

    assert client.post("/hr/audit/export/").status_code == 403
    assert not AuditLog.objects.filter(action="AUDIT_EXPORTED").exists()


@pytest.mark.django_db
def test_operator_dashboard_explains_admin_only_audit_capability(client):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.get("/hr/")
    body = response.content.decode()

    assert response.status_code == 200
    assert 'aria-disabled="true"' in body
    assert "仅 HR 管理员可查看和导出审计日志" in body
