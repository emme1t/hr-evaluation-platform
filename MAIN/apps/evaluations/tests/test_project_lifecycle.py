from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.evaluations.forms.projects import ProjectForm
from apps.evaluations.models import EvaluationProject, EvaluationTask
from apps.evaluations.services.projects import (
    ProjectStateError,
    close_project_early,
    extend_project_deadline,
    normalize_project_rules,
    prepare_project,
)


def test_rule_snapshot_canonicalizes_form_decimals_to_two_places():
    assert normalize_project_rules(
        {
            "manager": Decimal("0.50000"),
            "same_department": Decimal("0.30000"),
            "cross_department": Decimal("0.20000"),
        }
    ) == {
        "manager": "0.50",
        "same_department": "0.30",
        "cross_department": "0.20",
        "required_groups": ["manager", "same_department", "cross_department"],
    }


@pytest.mark.django_db
def test_project_form_defaults_and_validates_exact_decimal_rule_total():
    unbound = ProjectForm()
    assert unbound.fields["manager_weight"].initial == "0.50"
    assert unbound.fields["same_department_weight"].initial == "0.30"
    assert unbound.fields["cross_department_weight"].initial == "0.20"

    data = {
        "name": "Fictional project",
        "deadline": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        "manager_weight": "0.50",
        "same_department_weight": "0.30",
        "cross_department_weight": "0.199",
    }
    invalid = ProjectForm(data=data)
    assert invalid.is_valid() is False
    assert "精确等于 1.00" in str(invalid.non_field_errors())

    data["cross_department_weight"] = "-0.20"
    data["same_department_weight"] = "0.70"
    negative = ProjectForm(data=data)
    assert negative.is_valid() is False
    assert "不能为负数" in str(negative.non_field_errors())


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["ready", "active"])
def test_ready_or_active_project_deadline_can_only_move_later(
    draft_project, hr_admin, status
):
    prepare_project(draft_project, hr_admin)
    draft_project.status = status
    draft_project.save(update_fields=["status"])
    old_deadline = draft_project.deadline
    later = old_deadline + timedelta(days=2)

    extend_project_deadline(draft_project, later, hr_admin)
    draft_project.refresh_from_db()
    assert draft_project.deadline == later

    with pytest.raises(ProjectStateError, match="截止时间只能向后延长"):
        extend_project_deadline(draft_project, later, hr_admin)


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["draft", "closed", "archived"])
def test_deadline_extension_rejects_invalid_project_states(draft_project, hr_admin, status):
    draft_project.status = status
    draft_project.save(update_fields=["status"])

    with pytest.raises(ProjectStateError):
        extend_project_deadline(
            draft_project, draft_project.deadline + timedelta(days=1), hr_admin
        )


@pytest.mark.django_db
def test_close_project_early_is_idempotent_and_marks_only_pending_tasks(active_project, hr_admin):
    submitted = active_project.tasks.get(relationship_type="manager")
    submitted.status = EvaluationTask.Status.SUBMITTED
    submitted.save(update_fields=["status"])

    close_project_early(active_project, hr_admin)
    close_project_early(active_project, hr_admin)

    active_project.refresh_from_db()
    assert active_project.status == "closed"
    assert active_project.tasks.filter(status="not_submitted").count() == 2
    submitted.refresh_from_db()
    assert submitted.status == "submitted"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["draft", "ready", "archived"])
def test_close_project_early_rejects_non_active_states(draft_project, hr_admin, status):
    draft_project.status = status
    draft_project.save(update_fields=["status"])

    with pytest.raises(ProjectStateError):
        close_project_early(draft_project, hr_admin)


@pytest.mark.django_db
def test_hr_project_views_are_read_only_on_get_and_mutate_only_on_post(
    client, hr_admin, draft_project
):
    client.force_login(hr_admin)
    preview_url = reverse("evaluations:project-preview", args=[draft_project.public_id])
    prepare_url = reverse("evaluations:project-prepare", args=[draft_project.public_id])

    before = (draft_project.tasks.count(), draft_project.status)
    response = client.get(preview_url)
    assert response.status_code == 200
    draft_project.refresh_from_db()
    assert before == (draft_project.tasks.count(), draft_project.status)

    assert client.get(prepare_url).status_code == 405
    assert client.post(prepare_url).status_code == 302
    draft_project.refresh_from_db()
    assert draft_project.status == "ready"


@pytest.mark.django_db
def test_project_mutations_require_hr_and_csrf(draft_project, user_with_role):
    prepare_url = reverse("evaluations:project-prepare", args=[draft_project.public_id])
    non_hr = Client()
    non_hr.force_login(user_with_role)
    assert non_hr.post(prepare_url).status_code == 403

    csrf_client = Client(enforce_csrf_checks=True)
    from tests.factories import create_hr_user

    csrf_client.force_login(create_hr_user("HR_OPERATOR"))
    assert csrf_client.post(prepare_url).status_code == 403
    draft_project.refresh_from_db()
    assert draft_project.status == "draft"


@pytest.mark.django_db
def test_deadline_and_close_views_reject_get(active_project, hr_admin, client):
    client.force_login(hr_admin)
    extend_url = reverse(
        "evaluations:project-extend-deadline", args=[active_project.public_id]
    )
    close_url = reverse("evaluations:project-close", args=[active_project.public_id])

    assert client.get(extend_url).status_code == 405
    assert client.get(close_url).status_code == 405
    active_project.refresh_from_db()
    assert active_project.status == EvaluationProject.Status.ACTIVE
