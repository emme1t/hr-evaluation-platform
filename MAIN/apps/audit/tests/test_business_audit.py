from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import AuditContractError, record_audit
from apps.evaluations.services.projects import (
    ProjectStateError,
    close_project_early,
    extend_project_deadline,
    prepare_project,
)
from apps.evaluations.services.submissions import submit_task
from apps.reporting.views import _serve_export, record_export_success
from apps.roster.models import Employee
from apps.roster.services import (
    create_employee_record,
    deactivate_employee,
    update_employee_record,
)
from tests.factories import create_project


def _employee_fields(token):
    return {
        "employee_no": f"TX-{token}",
        "name": f"Fictional Transaction Employee {token}",
        "corporate_email": f"tx-{token}@example.test",
        "department_level_1": "Fictional Center",
        "department_level_2": "Fictional Team",
        "category_id": None,
        "wecom_userid": "",
    }


@pytest.mark.django_db
def test_roster_create_update_and_deactivate_append_matching_events(hr_admin):
    from tests.factories import create_category

    category = create_category("TX-AUDIT", "Fictional Transaction Category")
    fields = _employee_fields("A")
    fields["category_id"] = category.pk

    employee = create_employee_record(actor=hr_admin, **fields)
    fields["name"] = "Fictional Updated Transaction Employee"
    update_employee_record(employee.pk, actor=hr_admin, **fields)
    deactivate_employee(employee.pk, actor=hr_admin)

    assert list(
        AuditLog.objects.filter(target_id=str(employee.public_id))
        .order_by("id")
        .values_list("action", flat=True)
    ) == ["EMPLOYEE_CREATED", "EMPLOYEE_UPDATED", "EMPLOYEE_DEACTIVATED"]


@pytest.mark.django_db(transaction=True)
def test_audit_failure_rolls_back_roster_business_write(hr_admin, monkeypatch):
    from apps.roster import services as roster_services
    from tests.factories import create_category

    category = create_category("TX-ROLLBACK", "Fictional Rollback Category")
    fields = _employee_fields("ROLLBACK")
    fields["category_id"] = category.pk

    def fail_audit(*args, **kwargs):
        raise AuditContractError("forced fictional audit failure")

    monkeypatch.setattr(roster_services, "record_audit", fail_audit)

    with pytest.raises(AuditContractError):
        create_employee_record(actor=hr_admin, **fields)

    assert not Employee.objects.filter(employee_no="TX-ROLLBACK").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("repeats", [2, 5, 10])
def test_prepare_and_close_repeats_create_one_event_per_transition(
    draft_project, hr_admin, repeats
):
    for _ in range(repeats):
        prepare_project(draft_project, hr_admin)

    draft_project.status = "active"
    draft_project.launched_at = timezone.now()
    draft_project.save(update_fields=["status", "launched_at"])
    for _ in range(repeats):
        close_project_early(draft_project, hr_admin)

    assert AuditLog.objects.filter(
        action="PROJECT_PREPARED", target_id=str(draft_project.public_id)
    ).count() == 1
    assert AuditLog.objects.filter(
        action="PROJECT_CLOSED", target_id=str(draft_project.public_id)
    ).count() == 1


@pytest.mark.django_db
def test_stale_deadline_tab_and_state_reversal_create_no_extra_event(
    active_project, hr_admin
):
    new_deadline = active_project.deadline + timedelta(days=1)
    extend_project_deadline(active_project, new_deadline, hr_admin)

    with pytest.raises(ProjectStateError):
        extend_project_deadline(active_project, new_deadline, hr_admin)
    with pytest.raises(ProjectStateError):
        extend_project_deadline(
            active_project, active_project.deadline - timedelta(days=1), hr_admin
        )

    assert AuditLog.objects.filter(
        action="PROJECT_DEADLINE_EXTENDED",
        target_id=str(active_project.public_id),
    ).count() == 1


@pytest.mark.django_db
def test_final_submission_and_repeat_share_one_business_and_audit_effect(
    own_task, evaluator_user
):
    evaluator_user.groups.add(Group.objects.get(name="EVALUATOR"))
    complete_answers = {
        item["snapshot_item_id"]: item["score_max"]
        for item in own_task.project_subject.template_snapshot["items"]
    }

    first = submit_task(own_task, evaluator_user, complete_answers, "submit-audit-key")
    second = submit_task(own_task, evaluator_user, complete_answers, "submit-audit-key")

    assert first.pk == second.pk
    assert AuditLog.objects.filter(
        action="EVALUATION_SUBMITTED", target_id=str(own_task.public_id)
    ).count() == 1


@pytest.mark.django_db
def test_dual_role_evaluator_submission_uses_evaluator_role_and_commits(
    own_task, evaluator_user
):
    evaluator_user.groups.add(
        Group.objects.get(name="EVALUATOR"), Group.objects.get(name="HR_ADMIN")
    )
    complete_answers = {
        item["snapshot_item_id"]: item["score_max"]
        for item in own_task.project_subject.template_snapshot["items"]
    }

    submission = submit_task(
        own_task, evaluator_user, complete_answers, "dual-role-submit"
    )

    own_task.refresh_from_db()
    event = AuditLog.objects.get(
        action="EVALUATION_SUBMITTED", target_id=str(own_task.public_id)
    )
    assert submission.is_final is True
    assert own_task.status == "submitted"
    assert event.effective_role == "EVALUATOR"


@pytest.mark.django_db
def test_cross_project_target_scope_fails_closed(
    own_task, evaluator_user
):
    evaluator_user.groups.add(Group.objects.get(name="EVALUATOR"))
    other_project = create_project(bind_summary_template=False)

    with pytest.raises(AuditContractError):
        record_audit(
            evaluator_user,
            "EVALUATION_SUBMITTED",
            own_task,
            {"submission": "final"},
            project=other_project,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "expected_action"),
    [
        ("summary", "REPORT_SUMMARY_EXPORTED"),
        ("raw", "REPORT_RAW_EXPORTED"),
        ("issues", "REPORT_EXCEPTION_EXPORTED"),
    ],
)
def test_task11_export_seam_binds_only_fixed_success_actions(
    hr_admin, kind, expected_action
):
    project = create_project(bind_summary_template=False)

    record_export_success(
        actor=hr_admin,
        project=project,
        kind=kind,
        artifact_sha256="a" * 64,
        artifact_size=128,
    )

    event = AuditLog.objects.get(target_id=str(project.public_id))
    assert event.action == expected_action


@pytest.mark.django_db
def test_export_response_preparation_failure_records_no_success(
    hr_admin, monkeypatch
):
    project = create_project(bind_summary_template=False)
    request = RequestFactory().post("/hr/reporting/export/")
    request.user = hr_admin

    def fail_response(*args, **kwargs):
        raise RuntimeError("forced fictional response preparation failure")

    monkeypatch.setattr("apps.reporting.views._download_response", fail_response)

    with pytest.raises(RuntimeError):
        _serve_export(
            request,
            project,
            kind="summary",
            generator=lambda: b"fictional-artifact",
            filename="fictional.xlsx",
            content_type="application/octet-stream",
        )

    assert not AuditLog.objects.filter(
        action="REPORT_SUMMARY_EXPORTED", target_id=str(project.public_id)
    ).exists()
