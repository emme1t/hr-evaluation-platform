import hashlib
from uuid import uuid4

import pytest
from django.urls import reverse

from tests.factories import create_user_with_role


pytestmark = pytest.mark.django_db


@pytest.fixture
def export_success_events(monkeypatch):
    import apps.reporting.views as views

    events = []

    def record(**event):
        events.append(event)

    monkeypatch.setattr(views, "record_export_success", record)
    return events


def test_download_views_are_post_only_admin_only_and_private(
    client, project_results, hr_admin, export_success_events
):
    summary_url = reverse(
        "reporting:summary-download",
        kwargs={"project_id": project_results.project.public_id},
    )
    assert client.get(summary_url).status_code == 405

    client.force_login(create_user_with_role("EVALUATOR"))
    assert client.post(summary_url).status_code == 403
    client.force_login(hr_admin)
    response = client.post(summary_url)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, private"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "\r" not in response.headers["Content-Disposition"]
    assert "\n" not in response.headers["Content-Disposition"]
    assert len(export_success_events) == 1


def test_download_view_requires_csrf_when_success_creates_audit(
    project_results, hr_admin
):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(hr_admin)
    url = reverse(
        "reporting:summary-download",
        kwargs={"project_id": project_results.project.public_id},
    )
    assert client.post(url).status_code == 403


def test_successful_download_records_exactly_one_audit_after_artifact_ready(
    client, project_results, hr_admin, export_success_events
):
    client.force_login(hr_admin)
    url = reverse(
        "reporting:raw-download",
        kwargs={"project_id": project_results.project.public_id},
    )
    response = client.post(url)

    assert response.status_code == 200
    assert len(export_success_events) == 1
    event = export_success_events[0]
    assert event["kind"] == "raw"
    assert event["actor"] == hr_admin
    assert event["project"] == project_results.project
    assert event["artifact_size"] == len(response.content)
    assert event["artifact_sha256"] == hashlib.sha256(response.content).hexdigest()


def test_failed_generation_and_failed_response_preparation_record_no_success_audit(
    client, project_results, hr_admin, monkeypatch, export_success_events
):
    import apps.reporting.views as views

    client.force_login(hr_admin)
    url = reverse(
        "reporting:summary-download",
        kwargs={"project_id": project_results.project.public_id},
    )
    snapshot = dict(project_results.project.summary_template_snapshot)
    snapshot["sha256"] = "0" * 64
    project_results.project.summary_template_snapshot = snapshot
    project_results.project.save(update_fields=["summary_template_snapshot"])
    assert client.post(url).status_code == 409
    assert export_success_events == []

    snapshot["sha256"] = project_results.project.summary_template.sha256
    project_results.project.summary_template_snapshot = snapshot
    project_results.project.save(update_fields=["summary_template_snapshot"])

    def fail_response(*args, **kwargs):
        raise RuntimeError("fictional response preparation failure")

    monkeypatch.setattr(views, "_download_response", fail_response)
    with pytest.raises(RuntimeError, match="fictional"):
        client.post(url)
    assert export_success_events == []


def test_cross_project_or_unknown_objects_fail_without_audit_or_identifier_leak(
    client, project_results, incomplete_result, hr_admin, export_success_events
):
    client.force_login(hr_admin)
    raw_subject_url = reverse(
        "reporting:raw-subject-download",
        kwargs={
            "project_id": project_results.project.public_id,
            "subject_id": incomplete_result.subject.public_id,
        },
    )
    response = client.post(raw_subject_url)
    assert response.status_code == 404
    assert str(incomplete_result.subject.public_id).encode() not in response.content
    unknown = reverse(
        "reporting:issues-download", kwargs={"project_id": uuid4()}
    )
    assert client.post(unknown).status_code == 404
    assert export_success_events == []


def test_rapid_downloads_only_append_audit_and_do_not_mutate_business_state(
    client, project_results, hr_admin, export_success_events
):
    client.force_login(hr_admin)
    project = project_results.project
    url = reverse(
        "reporting:issues-download", kwargs={"project_id": project.public_id}
    )
    before = (
        list(project.subjects.values()),
        list(project.tasks.values()),
        list(project.aggregate_results.values()),
    )
    responses = [client.post(url) for _ in range(3)]
    after = (
        list(project.subjects.values()),
        list(project.tasks.values()),
        list(project.aggregate_results.values()),
    )
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert len(export_success_events) == 3
    assert after == before


def test_default_task_11_endpoint_uses_task_12_central_audit_service(
    client, project_results, hr_admin
):
    from apps.audit.models import AuditLog

    client.force_login(hr_admin)
    url = reverse(
        "reporting:summary-download",
        kwargs={"project_id": project_results.project.public_id},
    )

    response = client.post(url)

    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment;")
    events = AuditLog.objects.filter(
        action="REPORT_SUMMARY_EXPORTED",
        target_id=str(project_results.project.public_id),
    )
    assert events.count() == 1
