import pytest
from django.contrib.auth.models import Group


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "path", "expected"),
    [
        ("EVALUATOR", "/hr/", 403),
        ("HR_OPERATOR", "/hr/", 200),
        ("HR_OPERATOR", "/hr/audit/", 403),
        ("HR_ADMIN", "/hr/audit/", 200),
    ],
)
def test_hr_role_matrix(client, role, path, expected, user_with_role):
    user_with_role.groups.clear()
    user_with_role.groups.add(Group.objects.get(name=role))
    client.force_login(user_with_role)

    assert client.get(path).status_code == expected


@pytest.mark.django_db
def test_hr_permission_reloads_role_after_session_role_change(client, hr_admin):
    client.force_login(hr_admin)
    assert client.get("/hr/").status_code == 200

    hr_admin.groups.clear()

    assert client.get("/hr/").status_code == 403


@pytest.mark.django_db
def test_anonymous_hr_access_redirects_to_login_without_disclosing_data(client):
    response = client.get("/hr/")

    assert response.status_code == 302
    assert response.url.startswith("/auth/wecom/start/?next=")
