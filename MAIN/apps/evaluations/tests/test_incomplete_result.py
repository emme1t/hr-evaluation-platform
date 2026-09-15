from copy import deepcopy
from uuid import uuid4

import pytest

from apps.evaluations.models import Submission
from apps.evaluations.services.scoring import (
    ScoringIntegrityError,
    calculate_subject_result,
)


pytestmark = pytest.mark.django_db


def test_missing_required_group_leaves_total_blank(incomplete_subject_result_data):
    outcome = calculate_subject_result(**incomplete_subject_result_data)

    assert outcome.total_score is None
    assert outcome.status == "incomplete"
    assert outcome.missing_groups == ("manager",)
    assert outcome.group_scores["manager"] is None
    assert outcome.valid_submission_count == 2


def test_empty_subject_has_all_required_groups_missing(subject_result_builder):
    data = subject_result_builder({})

    outcome = calculate_subject_result(**data)

    assert outcome.total_score is None
    assert outcome.missing_groups == (
        "manager",
        "same_department",
        "cross_department",
    )
    assert outcome.valid_submission_count == 0


def test_draft_submission_does_not_make_group_present(incomplete_subject_result_data):
    manager_task = incomplete_subject_result_data["project"].tasks.get(
        relationship_type="manager"
    )
    Submission.objects.create(task=manager_task, is_final=False)

    outcome = calculate_subject_result(**incomplete_subject_result_data)

    assert outcome.missing_groups == ("manager",)
    assert outcome.valid_submission_count == 2


def test_expired_not_submitted_task_does_not_make_group_present(
    incomplete_subject_result_data,
):
    manager_task = incomplete_subject_result_data["project"].tasks.get(
        relationship_type="manager"
    )
    manager_task.status = manager_task.Status.NOT_SUBMITTED
    manager_task.save(update_fields=["status"])

    outcome = calculate_subject_result(**incomplete_subject_result_data)

    assert outcome.missing_groups == ("manager",)
    assert outcome.valid_submission_count == 2


def test_invalid_rule_snapshot_is_a_stable_domain_error(complete_subject_result_data):
    project = complete_subject_result_data["project"]
    project.rule_snapshot = {
        "manager": "0.50",
        "same_department": "0.30",
        "cross_department": "0.20",
    }
    project.save(update_fields=["rule_snapshot"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_subject_result(**complete_subject_result_data)

    assert raised.value.code == "PROJECT_FROZEN_DATA_INVALID"


def test_duplicate_relationship_snapshot_is_a_stable_domain_error(
    complete_subject_result_data,
):
    project_subject = complete_subject_result_data["project"].subjects.get()
    snapshot = deepcopy(project_subject.relationship_snapshot)
    snapshot.append(deepcopy(snapshot[0]))
    project_subject.relationship_snapshot = snapshot
    project_subject.save(update_fields=["relationship_snapshot"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_subject_result(**complete_subject_result_data)

    assert raised.value.code == "PROJECT_FROZEN_DATA_INVALID"


def test_task_evaluator_must_match_frozen_relationship_snapshot(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    manager_task = project.tasks.get(
        relationship_type="manager", evaluator__name__endswith="0"
    )
    other_evaluator = project.tasks.get(
        relationship_type="same_department"
    ).evaluator
    manager_task.evaluator = other_evaluator
    manager_task.save(update_fields=["evaluator"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_subject_result(**complete_subject_result_data)

    assert raised.value.code == "PROJECT_FROZEN_DATA_INVALID"


def test_corrupt_final_answers_abort_subject_calculation(complete_subject_result_data):
    submission = Submission.objects.filter(
        task__project=complete_subject_result_data["project"], is_final=True
    ).first()
    submission.answers.first().delete()

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_subject_result(**complete_subject_result_data)

    assert raised.value.code == "SUBMISSION_ANSWERS_INVALID"


def test_frozen_subject_identity_survives_current_employee_changes(
    complete_subject_result_data,
):
    project_subject = complete_subject_result_data["project"].subjects.get()
    frozen_name = project_subject.subject_snapshot["name"]
    frozen_public_id = project_subject.subject_snapshot["public_id"]
    subject = complete_subject_result_data["subject"]
    subject.name = "Renamed Current Employee"
    subject.is_active = False
    subject.save(update_fields=["name", "is_active"])

    outcome = calculate_subject_result(**complete_subject_result_data)

    assert outcome.subject_name == frozen_name
    assert str(outcome.subject_public_id) == frozen_public_id


def test_frozen_subject_public_id_must_match_linked_employee(
    complete_subject_result_data,
):
    project_subject = complete_subject_result_data["project"].subjects.get()
    snapshot = deepcopy(project_subject.subject_snapshot)
    snapshot["public_id"] = str(uuid4())
    project_subject.subject_snapshot = snapshot
    project_subject.save(update_fields=["subject_snapshot"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_subject_result(**complete_subject_result_data)

    assert raised.value.code == "PROJECT_FROZEN_DATA_INVALID"
