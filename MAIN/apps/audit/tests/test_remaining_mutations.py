from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import AuditContractError
from apps.evaluations.services.projects import prepare_project
from apps.evaluations.services.templates import create_template_version
from apps.notifications.services import launch_project, resend_project_notification
from apps.evaluations.models import EvaluationProject, FormTemplate
from apps.roster.imports import (
    ImportCommitError,
    commit_import_batch,
    preview_relationship_upload,
    preview_roster_upload,
)
from apps.roster.services import (
    create_category_record,
    deactivate_category,
    deactivate_relationship,
    update_category_record,
    update_relationship_record,
    upsert_relationship,
)
from tests.factories import create_category, create_employee


class SuccessfulNotifier:
    def send_task_summary(self, **kwargs):
        return None


class FailingNotifier:
    def send_task_summary(self, **kwargs):
        error = RuntimeError("fictional delivery failure")
        error.code = "FICTIONAL_DELIVERY_FAILED"
        raise error


def _template_form_payload(name, items, *, initial_forms):
    payload = {
        "name": name,
        "items-TOTAL_FORMS": str(len(items)),
        "items-INITIAL_FORMS": str(initial_forms),
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "1000",
    }
    for index, item in enumerate(items):
        for field, value in item.items():
            payload[f"items-{index}-{field}"] = str(value)
    return payload


@pytest.mark.django_db
def test_category_relationship_and_template_services_append_matching_actions(
    hr_admin, template_factory
):
    category = create_category_record(
        code="AUDIT-CATEGORY", name="Fictional Audit Category", actor=hr_admin
    )
    update_category_record(
        category.pk,
        code="AUDIT-CATEGORY",
        name="Fictional Updated Audit Category",
        actor=hr_admin,
    )

    subject = create_employee("REL-SUBJECT", "Fictional Relation Subject", category=category)
    peer = create_employee("REL-PEER", "Fictional Relation Peer", category=category)
    cross = create_employee(
        "REL-CROSS",
        "Fictional Relation Cross",
        category=category,
        department_level_1="Other Fictional Center",
        department_level_2="Other Fictional Team",
    )
    relationship = upsert_relationship(
        subject_no=subject.employee_no,
        evaluator_no=peer.employee_no,
        relationship_type="same_department",
        actor=hr_admin,
    )
    update_relationship_record(
        relationship.pk,
        subject_no=subject.employee_no,
        evaluator_no=cross.employee_no,
        relationship_type="cross_department",
        actor=hr_admin,
    )
    deactivate_relationship(relationship.pk, actor=hr_admin)

    template = template_factory(category=category)
    create_template_version(
        template,
        {"name": "Fictional Audit Template Version 2"},
        hr_admin,
    )
    deactivate_category(category.pk, actor=hr_admin)

    actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
    for expected in (
        "CATEGORY_CREATED",
        "CATEGORY_UPDATED",
        "RELATIONSHIP_CREATED",
        "RELATIONSHIP_UPDATED",
        "RELATIONSHIP_DEACTIVATED",
        "TEMPLATE_CREATED",
        "TEMPLATE_SEALED",
        "TEMPLATE_VERSION_CREATED",
        "CATEGORY_DEACTIVATED",
    ):
        assert expected in actions
    assert actions.count("TEMPLATE_SEALED") == 2


@pytest.mark.django_db(transaction=True)
def test_template_audit_failure_rolls_back_sealed_template(
    hr_admin, monkeypatch
):
    from apps.evaluations.services import templates as template_services
    from tests.factories import create_template

    category = create_category("AUDIT-TPL-RB", "Fictional Template Rollback")
    first = create_template(category=category, created_by=hr_admin)
    before = first.__class__.objects.filter(category=category).count()

    def fail_audit(*args, **kwargs):
        raise AuditContractError("forced fictional template audit failure")

    monkeypatch.setattr(template_services, "record_audit", fail_audit)
    with pytest.raises(AuditContractError):
        create_template_version(first, {"name": "Must Roll Back"}, hr_admin)

    assert first.__class__.objects.filter(category=category).count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["roster", "relationship"])
def test_import_commit_appends_one_batch_event_and_repeat_is_rejected(
    kind,
    hr_admin,
    employee_set,
    roster_workbook_bytes,
    relationship_workbook_bytes,
):
    if kind == "roster":
        batch = preview_roster_upload(roster_workbook_bytes, "fictional-roster.xlsx", hr_admin)
        action = "ROSTER_IMPORT_COMMITTED"
    else:
        batch = preview_relationship_upload(
            relationship_workbook_bytes, "fictional-relationships.xlsx", hr_admin
        )
        action = "RELATIONSHIP_IMPORT_COMMITTED"

    commit_import_batch(
        batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
    )
    with pytest.raises(ImportCommitError):
        commit_import_batch(
            batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
        )

    assert AuditLog.objects.filter(
        action=action, target_id=str(batch.public_id)
    ).count() == 1


@pytest.mark.django_db
def test_project_create_view_uses_prg_and_appends_project_created(
    client, draft_project, hr_admin
):
    subject_ids = list(draft_project.subjects.values_list("subject_id", flat=True))
    client.force_login(hr_admin)
    mutation_key = client.get("/hr/projects/new/").context["mutation_key"]

    response = client.post(
        "/hr/projects/new/",
        {
            "mutation_key": mutation_key,
            "name": "Fictional Audited Project",
            "deadline": (timezone.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"),
            "subjects": subject_ids,
            "manager_weight": "0.50",
            "same_department_weight": "0.30",
            "cross_department_weight": "0.20",
        },
    )

    assert response.status_code == 302
    event = AuditLog.objects.get(action="PROJECT_CREATED")
    assert response.url.endswith(f"/{event.target_id}/preview/")


@pytest.mark.django_db
@pytest.mark.parametrize("mutation_key", [None, "not-a-uuid"])
def test_project_create_rejects_missing_or_malformed_mutation_key_without_write(
    client, draft_project, hr_admin, mutation_key
):
    subject_ids = list(draft_project.subjects.values_list("subject_id", flat=True))
    client.force_login(hr_admin)
    payload = {
        "name": "Fictional Rejected Project",
        "deadline": (timezone.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"),
        "subjects": subject_ids,
        "manager_weight": "0.50",
        "same_department_weight": "0.30",
        "cross_department_weight": "0.20",
    }
    if mutation_key is not None:
        payload["mutation_key"] = mutation_key
    before = EvaluationProject.objects.count()

    response = client.post("/hr/projects/new/", payload)

    assert response.status_code == 400
    assert EvaluationProject.objects.count() == before
    assert not AuditLog.objects.filter(action="PROJECT_CREATED").exists()


@pytest.mark.django_db
def test_template_create_keeps_valid_mutation_key_across_form_correction(
    client, hr_admin
):
    client.force_login(hr_admin)
    page = client.get("/hr/templates/new/")
    mutation_key = page.context["mutation_key"]

    response = client.post(
        "/hr/templates/new/",
        {
            "mutation_key": mutation_key,
            "name": "Incomplete fictional template",
            "items-TOTAL_FORMS": "0",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
        },
    )

    assert response.status_code == 400
    assert response.context["mutation_key"] == mutation_key
    assert f'name="mutation_key" value="{mutation_key}"' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["create", "version"])
@pytest.mark.parametrize("mutation_key", [None, "not-a-uuid"])
def test_template_post_rejects_missing_or_malformed_mutation_key_without_write(
    client, hr_admin, template_v1, endpoint, mutation_key
):
    client.force_login(hr_admin)
    if endpoint == "create":
        category = create_category("REPLAY-CONTROL", "Replay Control Category")
        items = template_v1.item_payloads()
        payload = _template_form_payload(
            "Rejected fictional first template", items, initial_forms=0
        )
        payload["category"] = str(category.pk)
        url = "/hr/templates/new/"
        action = "TEMPLATE_CREATED"
    else:
        payload = _template_form_payload(
            "Rejected fictional next version",
            template_v1.item_payloads(),
            initial_forms=template_v1.items.count(),
        )
        url = f"/hr/templates/{template_v1.public_id}/edit/"
        action = "TEMPLATE_VERSION_CREATED"
    if mutation_key is not None:
        payload["mutation_key"] = mutation_key
    before = FormTemplate.objects.count()
    audit_before = AuditLog.objects.filter(action=action).count()

    response = client.post(url, payload)

    assert response.status_code == 400
    assert FormTemplate.objects.count() == before
    assert AuditLog.objects.filter(action=action).count() == audit_before


@pytest.mark.django_db
@pytest.mark.parametrize("repeats", [2, 5, 10])
def test_template_create_repeated_token_has_one_template_and_audit(
    client, hr_admin, template_v1, repeats
):
    category = create_category(
        f"REPLAY-{repeats}", f"Replay Template Category {repeats}"
    )
    client.force_login(hr_admin)
    mutation_key = client.get("/hr/templates/new/").context["mutation_key"]
    payload = _template_form_payload(
        f"Replay-safe first template {repeats}",
        template_v1.item_payloads(),
        initial_forms=0,
    )
    payload.update(category=str(category.pk), mutation_key=mutation_key)

    responses = [client.post("/hr/templates/new/", payload) for _ in range(repeats)]

    assert all(response.status_code == 302 for response in responses)
    assert FormTemplate.objects.filter(category=category).count() == 1
    assert AuditLog.objects.filter(
        action="TEMPLATE_CREATED",
        idempotency_key__contains=mutation_key,
    ).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("repeats", [2, 5, 10])
def test_template_version_repeated_token_has_one_version_and_audit(
    client, hr_admin, template_v1, repeats
):
    client.force_login(hr_admin)
    url = f"/hr/templates/{template_v1.public_id}/edit/"
    mutation_key = client.get(url).context["mutation_key"]
    items = template_v1.item_payloads()
    payload = {
        "mutation_key": mutation_key,
        "name": f"Fictional replay-safe template {repeats}",
        "items-TOTAL_FORMS": str(len(items)),
        "items-INITIAL_FORMS": str(len(items)),
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "1000",
    }
    for index, item in enumerate(items):
        for field, value in item.items():
            payload[f"items-{index}-{field}"] = str(value)

    responses = [client.post(url, payload) for _ in range(repeats)]

    assert all(response.status_code == 302 for response in responses)
    assert template_v1.__class__.objects.filter(category=template_v1.category).count() == 2
    assert AuditLog.objects.filter(
        action="TEMPLATE_VERSION_CREATED",
        idempotency_key__contains=mutation_key,
    ).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("repeats", [2, 5, 10])
def test_project_create_repeated_form_token_has_one_business_and_audit_effect(
    client, draft_project, hr_admin, repeats
):
    subject_ids = list(draft_project.subjects.values_list("subject_id", flat=True))
    client.force_login(hr_admin)
    form_page = client.get("/hr/projects/new/")
    mutation_key = form_page.context["mutation_key"]
    payload = {
        "mutation_key": mutation_key,
        "name": f"Fictional Repeated Project {repeats}",
        "deadline": (timezone.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"),
        "subjects": subject_ids,
        "manager_weight": "0.50",
        "same_department_weight": "0.30",
        "cross_department_weight": "0.20",
    }

    responses = [client.post("/hr/projects/new/", payload) for _ in range(repeats)]

    assert all(response.status_code == 302 for response in responses)
    assert len({response.url for response in responses}) == 1
    assert EvaluationProject.objects.filter(name=payload["name"]).count() == 1
    assert AuditLog.objects.filter(
        action="PROJECT_CREATED", idempotency_key__contains=mutation_key
    ).count() == 1


@pytest.mark.django_db
def test_launch_delivery_retry_and_outcome_events_share_authorized_project_scope(
    draft_project, hr_admin, django_capture_on_commit_callbacks
):
    prepare_project(draft_project, hr_admin)
    with django_capture_on_commit_callbacks(execute=True):
        launch_project(draft_project, hr_admin, notifier=FailingNotifier())
    failed_attempt = draft_project.notification_attempts.order_by("attempt").first()

    successful = resend_project_notification(
        draft_project,
        failed_attempt.recipient,
        "wecom",
        hr_admin,
        notifier=SuccessfulNotifier(),
        expected_attempt=str(failed_attempt.public_id),
    )

    assert AuditLog.objects.filter(
        action="PROJECT_LAUNCHED", target_id=str(draft_project.public_id)
    ).count() == 1
    assert AuditLog.objects.filter(
        action="NOTIFICATION_FAILED", target_id=str(failed_attempt.public_id)
    ).count() == 1
    assert AuditLog.objects.filter(
        action="NOTIFICATION_RETRIED", target_id=str(successful.public_id)
    ).count() == 1
    assert AuditLog.objects.filter(
        action="NOTIFICATION_SENT", target_id=str(successful.public_id)
    ).count() == 1
    assert all(
        event.target_project_id == str(draft_project.public_id)
        for event in AuditLog.objects.filter(action__startswith="NOTIFICATION_")
    )


@pytest.mark.django_db(transaction=True)
def test_launch_audit_failure_rolls_back_project_state(
    draft_project, hr_admin, monkeypatch
):
    from apps.notifications import services as notification_services

    prepare_project(draft_project, hr_admin)

    def fail_audit(*args, **kwargs):
        raise AuditContractError("forced fictional launch audit failure")

    monkeypatch.setattr(notification_services, "record_audit", fail_audit)
    with pytest.raises(AuditContractError):
        launch_project(draft_project, hr_admin, notifier=SuccessfulNotifier())

    draft_project.refresh_from_db()
    assert draft_project.status == "ready"
    assert draft_project.launched_at is None


@pytest.mark.django_db
def test_notification_retry_rejects_replayed_stale_tab_without_second_attempt(
    client,
    draft_project,
    hr_admin,
    django_capture_on_commit_callbacks,
    monkeypatch,
):
    from apps.notifications import services as notification_services

    prepare_project(draft_project, hr_admin)
    with django_capture_on_commit_callbacks(execute=True):
        launch_project(draft_project, hr_admin, notifier=FailingNotifier())
    outbox = draft_project.notification_outboxes.order_by("id").first()
    expected_attempt = str(outbox.attempts.get().public_id)
    monkeypatch.setattr(
        notification_services, "WeComNotifier", lambda: FailingNotifier()
    )
    client.force_login(hr_admin)

    first = client.post(
        f"/hr/notifications/{outbox.public_id}/retry/",
        {"expected_attempt": expected_attempt},
    )
    replay = client.post(
        f"/hr/notifications/{outbox.public_id}/retry/",
        {"expected_attempt": expected_attempt},
    )

    assert first.status_code == 302
    assert replay.status_code == 409
    assert outbox.attempts.count() == 2
    assert AuditLog.objects.filter(
        action="NOTIFICATION_RETRIED", target_project_id=str(draft_project.public_id)
    ).count() == 1


@pytest.mark.django_db
def test_recompute_with_explicit_admin_actor_appends_project_event(
    project_results, hr_admin
):
    project = project_results.project

    call_command(
        "recompute_project",
        project=str(project.public_id),
        actor=str(hr_admin.public_id),
        verbosity=0,
    )

    assert AuditLog.objects.filter(
        action="PROJECT_RECOMPUTED", target_id=str(project.public_id)
    ).count() == 1
