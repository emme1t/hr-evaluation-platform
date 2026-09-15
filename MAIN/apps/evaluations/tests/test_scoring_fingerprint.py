from copy import deepcopy
import re
from uuid import uuid4

import pytest

from apps.evaluations.models import Submission
from apps.evaluations.services import scoring
from apps.evaluations.services.scoring import (
    ScoringIntegrityError,
    build_project_scoring_source,
    calculate_project_results,
    calculate_subject_result,
)


pytestmark = pytest.mark.django_db


def _source_rows(project):
    project_subjects = list(
        project.subjects.select_related("subject").order_by("pk")
    )
    tasks = list(
        project.tasks.select_related(
            "project", "project_subject", "evaluator"
        ).order_by("pk")
    )
    submissions = list(
        Submission.objects.filter(task__project=project, is_final=True)
        .prefetch_related("answers")
        .order_by("pk")
    )
    return project_subjects, tasks, submissions


def test_fingerprint_is_independent_of_row_and_relationship_list_order(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    project_subjects, tasks, submissions = _source_rows(project)
    first_source = build_project_scoring_source(
        project, project_subjects, tasks, submissions
    )
    first = calculate_project_results(first_source)[0][1]
    project_subjects[0].relationship_snapshot = list(
        reversed(project_subjects[0].relationship_snapshot)
    )
    reordered_source = build_project_scoring_source(
        project,
        list(reversed(project_subjects)),
        list(reversed(tasks)),
        list(reversed(submissions)),
    )
    reordered = calculate_project_results(reordered_source)[0][1]

    assert reordered.input_fingerprint == first.input_fingerprint


@pytest.mark.parametrize(
    "mutation",
    (
        "rule",
        "subject",
        "template",
        "relationship",
        "task",
        "submission",
        "answer",
    ),
)
def test_every_scoring_input_mutation_changes_fingerprint(
    complete_subject_result_data, mutation
):
    project = complete_subject_result_data["project"]
    before = calculate_subject_result(**complete_subject_result_data)
    project_subject = project.subjects.get()
    if mutation == "rule":
        project.rule_snapshot = {
            "manager": "0.40",
            "same_department": "0.40",
            "cross_department": "0.20",
            "required_groups": [
                "manager",
                "same_department",
                "cross_department",
            ],
        }
        project.save(update_fields=["rule_snapshot"])
    elif mutation == "subject":
        snapshot = deepcopy(project_subject.subject_snapshot)
        snapshot["name"] += " changed"
        project_subject.subject_snapshot = snapshot
        project_subject.save(update_fields=["subject_snapshot"])
    elif mutation == "template":
        snapshot = deepcopy(project_subject.template_snapshot)
        snapshot["items"][0]["title"] += " changed"
        project_subject.template_snapshot = snapshot
        project_subject.save(update_fields=["template_snapshot"])
    elif mutation == "relationship":
        snapshot = deepcopy(project_subject.relationship_snapshot)
        snapshot[0]["evaluator"]["name"] += " changed"
        project_subject.relationship_snapshot = snapshot
        project_subject.save(update_fields=["relationship_snapshot"])
    elif mutation == "task":
        task = project.tasks.order_by("pk").first()
        task.public_id = uuid4()
        task.save(update_fields=["public_id"])
    elif mutation == "submission":
        submission = Submission.objects.filter(task__project=project).first()
        submission.public_id = uuid4()
        submission.save(update_fields=["public_id"])
    else:
        submission = Submission.objects.filter(task__project=project).first()
        answer = submission.answers.first()
        answer.score = 4 if answer.score != 4 else 3
        answer.save(update_fields=["score"])

    after = calculate_subject_result(**complete_subject_result_data)

    assert after.input_fingerprint != before.input_fingerprint


def test_current_employee_changes_do_not_change_fingerprint(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    before = calculate_subject_result(**complete_subject_result_data)
    employees = [complete_subject_result_data["subject"]] + [
        task.evaluator for task in project.tasks.select_related("evaluator")
    ]
    for index, employee in enumerate(employees):
        employee.name = f"Current employee renamed {index}"
        employee.is_active = False
        employee.save(update_fields=["name", "is_active"])

    after = calculate_subject_result(**complete_subject_result_data)

    assert after.subject_name == before.subject_name
    assert after.input_fingerprint == before.input_fingerprint


def test_fingerprint_is_lowercase_sha256(complete_subject_result_data):
    outcome = calculate_subject_result(**complete_subject_result_data)

    assert re.fullmatch(r"[0-9a-f]{64}", outcome.input_fingerprint)


def test_algorithm_version_changes_fingerprint(
    complete_subject_result_data, monkeypatch
):
    before = calculate_subject_result(**complete_subject_result_data)
    monkeypatch.setattr(scoring, "ALGORITHM_VERSION", "50-30-20-v2-test")

    after = calculate_subject_result(**complete_subject_result_data)

    assert after.input_fingerprint != before.input_fingerprint


def test_batch_source_rejects_cross_project_project_subject(
    subject_result_builder,
):
    first = subject_result_builder(
        {
            "manager": ((4, 4),),
            "same_department": ((4, 4),),
            "cross_department": ((4, 4),),
        }
    )
    second = subject_result_builder(
        {
            "manager": ((3, 3),),
            "same_department": ((3, 3),),
            "cross_department": ((3, 3),),
        }
    )
    _, tasks, submissions = _source_rows(first["project"])
    foreign_project_subjects, _, _ = _source_rows(second["project"])

    with pytest.raises(ScoringIntegrityError) as raised:
        build_project_scoring_source(
            first["project"],
            foreign_project_subjects,
            tasks,
            submissions,
        )

    assert raised.value.code == "PROJECT_FROZEN_DATA_INVALID"
