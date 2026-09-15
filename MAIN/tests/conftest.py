from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from tests.factories import (
    create_category,
    create_employee,
    create_hr_user,
    create_project,
    create_relationship,
    create_template,
    create_user_with_role,
)
from tests.helpers import build_relationship_workbook_bytes, build_roster_workbook_bytes


@pytest.fixture(autouse=True)
def force_test_environment(settings, tmp_path):
    settings.APP_ENV = "test"
    settings.SECRET_KEY = "test-only-secret-key-not-for-production"
    settings.REPORTING_PRIVATE_ROOT = tmp_path / "reporting-private"


@pytest.fixture
def hr_admin():
    return create_hr_user("HR_ADMIN")


@pytest.fixture
def template_bytes():
    from apps.reporting.tests.workbook_helpers import build_summary_template_bytes

    return build_summary_template_bytes(("P001",))


def _reporting_result_fixture(hr_admin, *, complete):
    from uuid import uuid4

    from apps.evaluations.models import AggregateResult
    from apps.evaluations.services.projects import prepare_project
    from apps.reporting.storage import read_private

    token = uuid4().hex[:10]
    category = create_category(f"REPORT-{token}", f"Report Category {token}")
    template = create_template(category=category, created_by=hr_admin)
    subject = create_employee(
        f"R-{token}",
        f"Report Subject {token}",
        category=category,
        corporate_email=f"report-{token}@example.test",
    )
    groups = ("manager", "same_department", "cross_department")
    evaluators = {}
    for group in groups:
        is_cross = group == "cross_department"
        evaluator = create_employee(
            f"RE-{token}-{group[:5]}",
            f"Report Evaluator {group} {token}",
            category=category,
            department_level_1="Other Center" if is_cross else "Test Center",
            department_level_2="Other Team" if is_cross else "Test Team",
        )
        evaluators[group] = evaluator
        create_relationship(
            subject=subject,
            evaluator=evaluator,
            relationship_type=group,
        )
    project = create_project(
        name=f"Report Project {token}",
        subject_templates=((subject, template),),
    )
    summary_template = project.summary_template
    source_template_bytes = read_private(summary_template.storage_path)
    prepare_project(project, hr_admin)
    project.status = "active"
    project.save(update_fields=["status"])
    project_subject = project.subjects.get()
    item_ids = [item["snapshot_item_id"] for item in project_subject.template_snapshot["items"]]
    submitted = {
        "same_department": (4, 3),
        "cross_department": (3, 3),
    }
    if complete:
        submitted["manager"] = (5, 5)
    for group, scores in submitted.items():
        task = project.tasks.get(relationship_type=group)
        from tests.factories import create_final_submission

        create_final_submission(task, dict(zip(item_ids, scores, strict=True)))
    call_command("recompute_project", project=str(project.public_id), verbosity=0)
    result = AggregateResult.objects.get(project=project)
    project.refresh_from_db()
    return SimpleNamespace(
        project=project,
        subject=subject,
        result=result,
        template_bytes=source_template_bytes,
        frozen_name=project_subject.subject_snapshot["name"],
    )


@pytest.fixture
def project_results(hr_admin):
    return _reporting_result_fixture(hr_admin, complete=True)


@pytest.fixture
def incomplete_result(hr_admin):
    return _reporting_result_fixture(hr_admin, complete=False)


@pytest.fixture
def user_with_role():
    return create_user_with_role("EVALUATOR")


@pytest.fixture
def employee_set():
    category = create_category("TEST", "Test Category")
    return [
        create_employee("E001", "Test Employee One", category=category),
        create_employee("E002", "Test Employee Two", category=category),
        create_employee(
            "E003",
            "Test Employee Three",
            department_level_1="Other Center",
            department_level_2="Other Team",
            category=category,
        ),
    ]


@pytest.fixture
def roster_workbook_bytes(employee_set):
    return build_roster_workbook_bytes(
        [
            (
                "E001",
                "Updated Employee One",
                "e001@example.test",
                "Test Center",
                "Test Team",
                "TEST",
                "wx_e001_updated",
            ),
            (
                "E004",
                "Test Employee Four",
                "e004@example.test",
                "Test Center",
                "Test Team",
                "TEST",
                "wx_e004",
            ),
        ]
    )


@pytest.fixture
def relationship_workbook_bytes(employee_set):
    return build_relationship_workbook_bytes(
        [
            ("E001", "E002", "same_department"),
            ("E001", "E003", "cross_department"),
            ("E002", "E001", "manager"),
        ]
    )


@pytest.fixture
def relationship_case_bytes(employee_set):
    cases = {
        "self": [("E001", "E001", "manager")],
        "missing": [("E001", "E999", "manager")],
        "same_department_mismatch": [("E001", "E003", "same_department")],
        "cross_department_mismatch": [("E001", "E002", "cross_department")],
        "duplicate": [
            ("E001", "E002", "same_department"),
            ("E001", "E002", "same_department"),
        ],
    }

    def build(case):
        return build_relationship_workbook_bytes(cases[case])

    return build


@pytest.fixture
def template_factory(hr_admin):
    counter = 0

    def build(item_weights=("0.60", "0.40"), **overrides):
        nonlocal counter
        counter += 1
        category = overrides.pop(
            "category", create_category(f"TPL{counter}", f"Template Category {counter}")
        )
        return create_template(
            category=category,
            created_by=overrides.pop("created_by", hr_admin),
            item_weights=item_weights,
            **overrides,
        )

    return build


@pytest.fixture
def template_v1(template_factory):
    return template_factory()


@pytest.fixture
def draft_project(hr_admin):
    category = create_category("PROJECT", "Project Test Category")
    subject = create_employee(
        "P001", "Project Subject", category=category, wecom_userid="wx_p001"
    )
    manager = create_employee(
        "P002", "Project Manager", category=category, wecom_userid="wx_p002"
    )
    peer = create_employee(
        "P003", "Project Peer", category=category, wecom_userid="wx_p003"
    )
    cross = create_employee(
        "P004",
        "Project Cross Team",
        department_level_1="Other Center",
        department_level_2="Other Team",
        category=category,
        wecom_userid="wx_p004",
    )
    template = create_template(category=category, created_by=hr_admin)
    create_relationship(subject=subject, evaluator=manager, relationship_type="manager")
    create_relationship(
        subject=subject, evaluator=peer, relationship_type="same_department"
    )
    create_relationship(
        subject=subject, evaluator=cross, relationship_type="cross_department"
    )
    return create_project(subject_templates=((subject, template),))


@pytest.fixture
def active_project(draft_project, hr_admin):
    from apps.evaluations.services.projects import prepare_project

    prepare_project(draft_project, hr_admin)
    draft_project.status = "active"
    draft_project.launched_at = draft_project.prepared_at
    draft_project.save(update_fields=["status", "launched_at"])
    draft_project.refresh_from_db()
    return draft_project


@pytest.fixture
def own_task(hr_admin):
    from apps.evaluations.services.projects import prepare_project

    category = create_category("TASKCENTER", "Task Center Category")
    template = create_template(category=category, created_by=hr_admin)
    own_subject = create_employee(
        "TASK-SUBJECT-OWN", "Own Task Subject", category=category
    )
    other_subject = create_employee(
        "TASK-SUBJECT-OTHER", "Other Task Subject", category=category
    )
    own_evaluator = create_employee(
        "TASK-EVALUATOR-OWN", "Own Evaluator", category=category
    )
    other_evaluator = create_employee(
        "TASK-EVALUATOR-OTHER", "Other Evaluator", category=category
    )
    peer = create_employee("TASK-PEER", "Task Peer", category=category)
    cross = create_employee(
        "TASK-CROSS",
        "Task Cross",
        category=category,
        department_level_1="Other Center",
        department_level_2="Other Team",
    )
    for subject, manager in (
        (own_subject, own_evaluator),
        (other_subject, other_evaluator),
    ):
        create_relationship(
            subject=subject, evaluator=manager, relationship_type="manager"
        )
        create_relationship(
            subject=subject, evaluator=peer, relationship_type="same_department"
        )
        create_relationship(
            subject=subject, evaluator=cross, relationship_type="cross_department"
        )
    project = create_project(
        name="Task Center Project",
        subject_templates=((own_subject, template), (other_subject, template)),
    )
    prepare_project(project, hr_admin)
    project.status = "active"
    project.launched_at = project.prepared_at
    project.save(update_fields=["status", "launched_at"])
    return project.tasks.get(evaluator=own_evaluator, subject=own_subject)


@pytest.fixture
def evaluator_user(own_task):
    user = get_user_model().objects.create_user(
        username="task-evaluator@example.test"
    )
    own_task.evaluator.user = user
    own_task.evaluator.save(update_fields=["user"])
    return user


@pytest.fixture
def other_task(own_task):
    return own_task.project.tasks.get(
        evaluator__employee_no="TASK-EVALUATOR-OTHER"
    )
