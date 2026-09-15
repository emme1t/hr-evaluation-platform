import logging

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.roster.models import Employee, EmployeeCategory

from .test_wecom_oauth import FakeWeComClient, set_oauth_session


def create_employee(**overrides):
    category = EmployeeCategory.objects.create(code="OPS", name="运营")
    defaults = {
        "employee_no": "E101",
        "name": "测试员工",
        "corporate_email": "e101@example.test",
        "category": category,
        "department_level_1": "运营",
        "department_level_2": "项目",
        "wecom_userid": "wx_e101",
    }
    defaults.update(overrides)
    return Employee.objects.create(**defaults)


@pytest.mark.django_db
def test_callback_creates_unusable_local_account_binds_and_logs_in_employee(client, monkeypatch):
    employee = create_employee()
    set_oauth_session(client)
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: FakeWeComClient())

    response = client.get(reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "ok"})

    employee.refresh_from_db()
    assert response.status_code == 302
    assert response.url == "/tasks/"
    assert employee.user_id is not None
    assert employee.user.has_usable_password() is False
    assert client.session["_auth_user_id"] == str(employee.user_id)


@pytest.mark.django_db
def test_callback_reuses_existing_bound_employee_account(client, monkeypatch):
    user = get_user_model().objects.create_user("e101@example.test")
    employee = create_employee(user=user)
    set_oauth_session(client)
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: FakeWeComClient())

    response = client.get(reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "ok"})

    assert response.status_code == 302
    assert get_user_model().objects.count() == 1
    assert client.session["_auth_user_id"] == str(employee.user_id)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("employee_overrides", "returned_user_id", "event"),
    [
        ({"is_active": False}, "wx_e101", "wecom_oauth_inactive_employee"),
        ({}, "wx_not_bound", "wecom_oauth_unbound_identity"),
    ],
)
def test_callback_refuses_ineligible_identity_without_sensitive_log_data(
    client, monkeypatch, caplog, employee_overrides, returned_user_id, event
):
    create_employee(**employee_overrides)
    set_oauth_session(client)
    monkeypatch.setattr(
        "apps.accounts.views.get_wecom_client", lambda: FakeWeComClient(returned_user_id)
    )

    with caplog.at_level(logging.WARNING, logger="apps.accounts.views"):
        response = client.get(
            reverse("accounts:wecom_callback"),
            {"state": "valid-state", "code": "code-that-must-not-leak"},
        )

    assert response.status_code == 403
    assert "登录失败，请联系管理员。" in response.content.decode()
    assert event in caplog.text
    assert returned_user_id not in caplog.text
    assert "code-that-must-not-leak" not in caplog.text


@pytest.mark.django_db
def test_callback_hides_wecom_api_failure_and_logs_stable_event(client, monkeypatch, caplog):
    set_oauth_session(client)
    monkeypatch.setattr(
        "apps.accounts.views.get_wecom_client",
        lambda: FakeWeComClient(error=RuntimeError("token-and-code-must-not-leak")),
    )

    with caplog.at_level(logging.WARNING, logger="apps.accounts.views"):
        response = client.get(
            reverse("accounts:wecom_callback"),
            {"state": "valid-state", "code": "code-that-must-not-leak"},
        )

    assert response.status_code == 403
    assert "登录失败，请联系管理员。" in response.content.decode()
    assert "wecom_oauth_identity_fetch_failed" in caplog.text
    assert "token-and-code-must-not-leak" not in caplog.text
    assert "code-that-must-not-leak" not in caplog.text
