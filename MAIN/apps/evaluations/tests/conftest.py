import pytest
from uuid import uuid4

from apps.evaluations.services.projects import prepare_project
from tests.factories import (
    create_category,
    create_employee,
    create_final_submission,
    create_project,
    create_relationship,
    create_template,
)


@pytest.fixture
def task(own_task):
    return own_task


@pytest.fixture
def complete_answers(task):
    return {
        item["snapshot_item_id"]: item["score_max"]
        for item in task.project_subject.template_snapshot["items"]
    }


@pytest.fixture
def final_submission(task):
    items = task.project_subject.template_snapshot["items"]
    return create_final_submission(
        task,
        {
            items[0]["snapshot_item_id"]: 5,
            items[1]["snapshot_item_id"]: 3,
        },
    )


@pytest.fixture
def subject_result_builder(hr_admin):
    def build(
        submitted_scores_by_group,
        *,
        item_weights=("0.50", "0.50"),
        evaluator_counts=None,
    ):
        token = uuid4().hex[:10]
        category = create_category(f"SCORE-{token}", f"Score Category {token}")
        template = create_template(
            category=category,
            created_by=hr_admin,
            item_weights=item_weights,
        )
        subject = create_employee(
            f"SUB-{token}", f"Score Subject {token}", category=category
        )
        evaluator_counts = evaluator_counts or {}
        required_groups = ("manager", "same_department", "cross_department")
        for group in required_groups:
            submitted = tuple(submitted_scores_by_group.get(group, ()))
            count = evaluator_counts.get(group, max(1, len(submitted)))
            for index in range(count):
                is_cross = group == "cross_department"
                evaluator = create_employee(
                    f"EV-{token}-{group[:5]}-{index}",
                    f"Score Evaluator {group} {index}",
                    department_level_1=("Other Center" if is_cross else "Test Center"),
                    department_level_2=("Other Team" if is_cross else "Test Team"),
                    category=category,
                )
                create_relationship(
                    subject=subject,
                    evaluator=evaluator,
                    relationship_type=group,
                )
        project = create_project(
            name=f"Score Project {token}",
            subject_templates=((subject, template),),
        )
        prepare_project(project, hr_admin)
        project.status = "active"
        project.save(update_fields=["status"])
        project.refresh_from_db()
        project_subject = project.subjects.get()
        item_ids = [
            item["snapshot_item_id"]
            for item in project_subject.template_snapshot["items"]
        ]
        for group, score_pairs in submitted_scores_by_group.items():
            tasks = list(project.tasks.filter(relationship_type=group).order_by("pk"))
            for task_row, scores in zip(tasks, score_pairs, strict=True):
                create_final_submission(
                    task_row, dict(zip(item_ids, scores, strict=True))
                )
        project.refresh_from_db()
        return {"project": project, "subject": subject}

    return build


@pytest.fixture
def complete_subject_result_data(subject_result_builder):
    return subject_result_builder(
        {
            "manager": ((5, 5), (3, 3)),
            "same_department": ((4, 3),),
            "cross_department": ((3, 3),),
        }
    )


@pytest.fixture
def incomplete_subject_result_data(subject_result_builder):
    return subject_result_builder(
        {
            "same_department": ((4, 3),),
            "cross_department": ((3, 3),),
        }
    )
