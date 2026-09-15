"""Factories for fake test data; populated by the relevant domain tasks."""

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

from apps.evaluations.models import (
    EvaluationProject,
    EvaluationTask,
    FormTemplate,
    ProjectSubject,
    TemplateItem,
)
from apps.evaluations.services.templates import (
    TemplateValidationError,
    create_initial_template,
)
from apps.roster.models import Employee, EmployeeCategory, EvaluationRelationship
from apps.roster.services import ensure_default_groups


def create_category(code: str, name: str) -> EmployeeCategory:
    return EmployeeCategory.objects.create(code=code, name=name)


def create_employee(
    employee_no: str,
    name: str,
    department_level_1: str = "Test Center",
    department_level_2: str = "Test Team",
    category: EmployeeCategory | None = None,
    corporate_email: str | None = None,
    wecom_userid: str | None = None,
    user=None,
    manager: Employee | None = None,
    is_active: bool = True,
) -> Employee:
    if category is None:
        category, _ = EmployeeCategory.objects.get_or_create(
            code="DEFAULT", defaults={"name": "Default Category"}
        )
    return Employee.objects.create(
        employee_no=employee_no,
        name=name,
        department_level_1=department_level_1,
        department_level_2=department_level_2,
        category=category,
        corporate_email=corporate_email or f"{employee_no.lower()}@example.test",
        wecom_userid=wecom_userid,
        user=user,
        manager=manager,
        is_active=is_active,
    )


def create_user_with_role(role: str):
    ensure_default_groups()
    user = get_user_model().objects.create_user(
        username=f"user-{uuid4().hex}@example.test"
    )
    user.groups.add(user.groups.model.objects.get(name=role))
    return user


def create_hr_user(role: str):
    if role not in {"HR_ADMIN", "HR_OPERATOR"}:
        raise ValueError("role must be HR_ADMIN or HR_OPERATOR")
    return create_user_with_role(role)


def create_template(
    category: EmployeeCategory,
    version: int = 1,
    item_weights: tuple[str, ...] = ("0.60", "0.40"),
    *,
    created_by=None,
    item_orders: tuple[int, ...] | None = None,
    item_overrides: dict[int, dict] | None = None,
) -> FormTemplate:
    """Create a complete fictional template for tests only."""
    item_orders = item_orders or tuple(range(1, len(item_weights) + 1))
    item_overrides = item_overrides or {}
    actor = created_by or create_hr_user("HR_ADMIN")
    items = []
    for index, weight in enumerate(item_weights, start=1):
        values = {
            "group": "Test group",
            "title": f"Test item {index}",
            "order": item_orders[index - 1],
            "weight": weight,
            "score_min": 1,
            "score_max": 5,
            "excellent_description": "Excellent fictional result",
            "good_description": "Good fictional result",
            "qualified_description": "Qualified fictional result",
            "improvement_description": "Improvement fictional result",
        }
        values.update(item_overrides.get(index, {}))
        items.append(values)
    try:
        if version == 1:
            return create_initial_template(
                category=category,
                name=f"Test evaluation template v{version}",
                items=items,
                actor=actor,
            )
    except TemplateValidationError:
        pass
    template = FormTemplate(
        category=category,
        name=f"Test evaluation template v{version}",
        version=version,
        previous_version=None,
        created_by=actor,
    )
    models.Model.save(template, force_insert=True, using=FormTemplate.objects.db)
    TemplateItem.objects.bulk_create(
        [TemplateItem(template=template, **values) for values in items]
    )
    return template


def create_relationship(
    *, subject: Employee, evaluator: Employee, relationship_type: str, is_active=True
) -> EvaluationRelationship:
    return EvaluationRelationship.objects.create(
        subject=subject,
        evaluator=evaluator,
        relationship_type=relationship_type,
        is_active=is_active,
    )


def create_project(
    *,
    name: str = "Fictional evaluation project",
    status: str = EvaluationProject.Status.DRAFT,
    deadline=None,
    rule_snapshot: dict | None = None,
    subject_templates: tuple[tuple[Employee, FormTemplate], ...] = (),
    bind_summary_template: bool = True,
) -> EvaluationProject:
    initial_status = (
        EvaluationProject.Status.DRAFT if bind_summary_template else status
    )
    project = EvaluationProject.objects.create(
        name=name,
        status=initial_status,
        deadline=deadline or timezone.now() + timedelta(days=14),
        rule_snapshot=rule_snapshot
        or {
            "manager": "0.50",
            "same_department": "0.30",
            "cross_department": "0.20",
        },
    )
    ProjectSubject.objects.bulk_create(
        [
            ProjectSubject(project=project, subject=subject, template=template)
            for subject, template in subject_templates
        ]
    )
    if bind_summary_template:
        from apps.reporting.services.summary import (
            assign_summary_template,
            register_summary_template,
        )
        from apps.reporting.tests.workbook_helpers import build_summary_template_bytes

        actor = (
            subject_templates[0][1].created_by
            if subject_templates
            else create_hr_user("HR_ADMIN")
        )
        payload = build_summary_template_bytes(
            tuple(subject.employee_no for subject, _template in subject_templates)
        )
        summary_template = register_summary_template(
            payload,
            f"fictional-summary-{project.public_id}.xlsx",
            actor,
        )
        assign_summary_template(project, summary_template, actor)
        if status != EvaluationProject.Status.DRAFT:
            project.status = status
            project.save(update_fields=["status"])
    return project


def create_task(
    *,
    project: EvaluationProject,
    evaluator: Employee,
    subject: Employee,
    relationship_type: str,
    status: str = EvaluationTask.Status.PENDING,
) -> EvaluationTask:
    project_subject, _ = ProjectSubject.objects.get_or_create(
        project=project,
        subject=subject,
        defaults={"template": FormTemplate.objects.get(category=subject.category)},
    )
    return EvaluationTask.objects.create(
        project=project,
        project_subject=project_subject,
        evaluator=evaluator,
        subject=subject,
        relationship_type=relationship_type,
        status=status,
    )


def create_final_submission(
    task: EvaluationTask, item_scores: dict[str, int]
):
    """Submit one complete fictional evaluation through the Task 8 service."""
    from apps.evaluations.services.submissions import submit_task

    if task.evaluator.user_id is None:
        task.evaluator.user = get_user_model().objects.create_user(
            username=f"evaluator-{uuid4().hex}@example.test"
        )
        task.evaluator.save(update_fields=["user"])
    task.project.status = EvaluationProject.Status.ACTIVE
    task.project.deadline = timezone.now() + timedelta(days=14)
    task.project.save(update_fields=["status", "deadline"])
    return submit_task(
        task,
        task.evaluator.user,
        item_scores,
        f"test-{uuid4().hex}",
    )
