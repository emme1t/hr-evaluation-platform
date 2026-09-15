from datetime import timedelta
import html
from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.audit.models import AuditLog
from apps.audit.services import record_audit
from apps.notifications.models import NotificationAttempt, NotificationOutbox
from apps.roster.models import Employee
from tests.factories import create_employee, create_project, create_user_with_role


class _LandmarkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.start_tags = []

    def handle_starttag(self, tag, attrs):
        self.start_tags.append((tag, dict(attrs)))


def _attempt(outbox, number, status):
    return NotificationAttempt.objects.create(
        outbox=outbox,
        project=outbox.project,
        recipient=outbox.recipient,
        task_count=outbox.task_count,
        channel="wecom",
        status=status,
        attempt=number,
        payload_snapshot={"version": 1, "channel": "wecom"},
    )


@pytest.mark.django_db
def test_dashboard_metrics_use_live_canonical_rows_and_latest_attempt(
    client, active_project
):
    tasks = list(active_project.tasks.select_related("evaluator").order_by("id"))
    type(tasks[0]).objects.filter(pk=tasks[0].pk).update(status="submitted")
    type(active_project).objects.filter(pk=active_project.pk).update(
        deadline=timezone.now() - timedelta(minutes=1)
    )

    first = NotificationOutbox.objects.create(
        project=active_project,
        recipient=tasks[0].evaluator,
        task_count=1,
        task_digest="a" * 64,
        status="sent",
    )
    first.tasks.add(tasks[0])
    _attempt(first, 1, "failed")
    _attempt(first, 2, "sent")

    second = NotificationOutbox.objects.create(
        project=active_project,
        recipient=tasks[1].evaluator,
        task_count=1,
        task_digest="b" * 64,
        status="failed",
    )
    second.tasks.add(tasks[1])
    _attempt(second, 1, "sent")
    _attempt(second, 2, "failed")

    create_project(status="closed", bind_summary_template=False)
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.get("/hr/")

    assert response.status_code == 200
    metrics = response.context["metrics"]
    assert metrics == {
        "employee_count": Employee.objects.filter(is_active=True).count(),
        "active_projects": 1,
        "completion_numerator": 1,
        "completion_denominator": 3,
        "pending_tasks": 2,
        "failed_outboxes": 1,
        "anomalies": 1,
    }


@pytest.mark.django_db
def test_all_dashboard_metrics_link_to_reachable_role_appropriate_pages(
    client, active_project
):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.get(reverse("audit:dashboard"))

    assert response.status_code == 200
    links = response.context["metric_links"]
    assert set(links) == {
        "employee_count",
        "active_projects",
        "completion",
        "pending_tasks",
        "failed_outboxes",
        "anomalies",
    }
    body = html.unescape(response.content.decode())
    for url in links.values():
        assert f'href="{url}"' in body
        assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_workbench_navigation_uses_real_routes_and_role_boundaries(client, hr_admin):
    operator = create_user_with_role("HR_OPERATOR")
    operator_routes = [
        reverse("roster:roster-list"),
        reverse("roster:category-list"),
        reverse("roster:relationship-list"),
        reverse("evaluations:template-list"),
        reverse("evaluations:project-list"),
    ]
    client.force_login(operator)
    operator_page = client.get(reverse("audit:dashboard"))
    operator_body = operator_page.content.decode()
    for url in operator_routes:
        assert f'href="{url}"' in operator_body
        assert client.get(url).status_code == 200

    reporting_url = reverse("reporting:template-list")
    assert reporting_url not in operator_body
    assert client.get(reporting_url).status_code == 403

    client.force_login(hr_admin)
    admin_page = client.get(reverse("audit:dashboard"))
    assert f'href="{reporting_url}"' in admin_page.content.decode()
    assert client.get(reporting_url).status_code == 200


@pytest.mark.django_db
def test_project_workbench_pages_share_one_accessible_application_shell(
    client, active_project
):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)
    urls = [
        reverse("evaluations:project-create"),
        reverse("evaluations:project-preview", args=[active_project.public_id]),
        reverse("evaluations:project-detail", args=[active_project.public_id]),
    ]

    for url in urls:
        response = client.get(url)
        parser = _LandmarkParser()
        parser.feed(response.content.decode())
        main_landmarks = [attrs for tag, attrs in parser.start_tags if tag == "main"]
        navigation_landmarks = [
            attrs for tag, attrs in parser.start_tags if tag == "nav"
        ]
        skip_links = [
            attrs
            for tag, attrs in parser.start_tags
            if tag == "a" and attrs.get("href") == "#main"
        ]

        assert response.status_code == 200
        assert main_landmarks == [{"id": "main", "tabindex": "-1"}]
        assert navigation_landmarks == [{"aria-label": "HR 主导航"}]
        assert len(skip_links) == 1


@pytest.mark.django_db
def test_dashboard_zero_denominator_empty_state_and_server_rendered_semantics(client):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    response = client.get("/hr/")
    body = response.content.decode()

    assert response.status_code == 200
    assert response.context["metrics"]["completion_numerator"] == 0
    assert response.context["metrics"]["completion_denominator"] == 0
    assert "暂无近期活动" in body
    assert 'name="viewport"' in body
    assert 'href="#main"' in body
    assert 'tabindex="-1"' in body
    assert 'aria-busy="false"' in body
    assert 'data-state="ready"' in body
    assert "正在加载工作台" in body
    assert "@media" in body
    assert ":focus-visible" in body


@pytest.mark.django_db
def test_dashboard_database_error_has_nonleaking_error_state(client, monkeypatch):
    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)

    def fail_metrics():
        raise DatabaseError("fictional raw database detail")

    monkeypatch.setattr("apps.audit.views.dashboard_metrics", fail_metrics)
    response = client.get("/hr/")
    body = response.content.decode()

    assert response.status_code == 503
    assert 'role="alert"' in body
    assert 'data-state="error"' in body
    assert "工作台暂时不可用" in body
    assert "fictional raw database detail" not in body


@pytest.mark.django_db
def test_recent_activity_is_role_filtered_limited_and_links_to_reachable_routes(
    client, hr_admin
):
    employee = create_employee("RECENT-001", "Fictional Recent Employee")
    project = create_project(bind_summary_template=False)
    for index in range(10):
        record_audit(
            hr_admin,
            "EMPLOYEE_UPDATED",
            employee,
            {"sequence": index},
            idempotency_key=f"recent-{index}",
        )
    record_audit(
        hr_admin,
        "REPORT_RAW_EXPORTED",
        project,
        {"artifact_size": 1},
        idempotency_key="recent-admin-only",
    )

    operator = create_user_with_role("HR_OPERATOR")
    client.force_login(operator)
    response = client.get("/hr/")
    activities = response.context["recent_activity"]
    body = response.content.decode()

    assert len(activities) == 8
    assert all(item.event.action == "EMPLOYEE_UPDATED" for item in activities)
    assert "REPORT_RAW_EXPORTED" not in body
    assert "recent-admin-only" not in body
    for item in activities:
        assert item.url == "/hr/roster/"
        assert client.get(item.url).status_code == 200


@pytest.mark.django_db
def test_admin_recent_activity_can_see_evaluator_business_event(
    client, hr_admin, own_task, evaluator_user
):
    evaluator_user.groups.add(Group.objects.get(name="EVALUATOR"))
    record_audit(
        evaluator_user,
        "EVALUATION_SUBMITTED",
        own_task,
        {"submission": "final"},
        idempotency_key="admin-sees-evaluator-event",
        project=own_task.project,
    )
    client.force_login(hr_admin)

    response = client.get("/hr/")

    assert response.status_code == 200
    assert "EVALUATION_SUBMITTED" in [
        item.event.action for item in response.context["recent_activity"]
    ]


@pytest.mark.django_db
def test_audit_query_has_stable_fifty_row_pagination_and_filter_switching(
    client, hr_admin
):
    employee = create_employee("QUERY-001", "Fictional Query Employee")
    with freeze_time("2026-08-01 08:00:00+00:00"):
        for index in range(55):
            record_audit(
                hr_admin,
                "EMPLOYEE_UPDATED",
                employee,
                {"sequence": index},
                idempotency_key=f"query-{index}",
            )
    client.force_login(hr_admin)

    first = client.get("/hr/audit/?start=2026-05-01&end=2026-08-01")
    second = client.get("/hr/audit/?start=2026-05-01&end=2026-08-01&page=2")
    switched = client.get(
        "/hr/audit/?start=2026-05-01&end=2026-08-01&action=EMPLOYEE_CREATED"
    )

    assert first.status_code == second.status_code == switched.status_code == 200
    assert len(first.context["page_obj"].object_list) == 50
    assert len(second.context["page_obj"].object_list) == 5
    assert list(first.context["page_obj"].object_list) == list(
        AuditLog.objects.order_by("-created_at", "-id")[:50]
    )
    assert switched.context["page_obj"].paginator.count == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query",
    [
        "start=bad&end=2026-08-01",
        "start=2026-08-02&end=2026-08-01",
        "start=2026-04-30&end=2026-08-01",
        "start=2026-05-01&end=2026-08-01&action=UNKNOWN",
        "start=2026-05-01&end=2026-08-01&page=-1",
        "start=2026-05-01&end=2026-08-01&page=999999999999999999999",
        "start=2026-05-01&end=2026-08-01&page=１２",
        "start=2026-05-01&end=2026-08-01&page=" + "9" * 5000,
        "start=" + "2" * 5000 + "&end=2026-08-01",
        "start=2026-05-01&end=2026-08-01&operator=" + "a" * 5000,
    ],
)
def test_audit_query_rejects_malformed_unbounded_or_pathological_inputs(
    client, hr_admin, query
):
    client.force_login(hr_admin)

    response = client.get(f"/hr/audit/?{query}")

    assert response.status_code == 400
    assert "审计查询条件无效" in response.content.decode()
