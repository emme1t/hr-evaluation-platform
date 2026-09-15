from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.evaluations.models import EvaluationProject
from tests.factories import (
    create_category,
    create_employee,
    create_project,
    create_task,
    create_template,
)


@dataclass
class FakeWeComNotifier:
    failure_code: str | None = None
    calls: list[dict] = field(default_factory=list)

    def send_task_summary(self, **payload):
        self.calls.append(payload)
        if self.failure_code:
            error = RuntimeError("fictional external failure")
            error.code = self.failure_code
            raise error


@pytest.fixture
def notification_case(hr_admin):
    category = create_category("NOTICE", "Notification Test Category")
    template = create_template(category=category, created_by=hr_admin)
    evaluator = create_employee(
        "NOTICE-EVALUATOR",
        "通知测试评价人",
        category=category,
        corporate_email="notice-evaluator@example.test",
        wecom_userid="wx-notice-evaluator",
    )
    project = create_project(
        name="虚构季度评价项目",
        status=EvaluationProject.Status.READY,
        deadline=timezone.now() + timedelta(days=10),
    )
    project.prepared_at = timezone.now()
    project.save(update_fields=["prepared_at"])
    subjects = []
    for index, relationship_type in enumerate(
        ("manager", "same_department", "cross_department"), start=1
    ):
        subject = create_employee(
            f"NOTICE-SUBJECT-{index}",
            f"虚构被评价人{index}",
            category=category,
        )
        subjects.append(subject)
        create_task(
            project=project,
            evaluator=evaluator,
            subject=subject,
            relationship_type=relationship_type,
        )
    return project, evaluator, tuple(subjects)


@pytest.fixture(autouse=True)
def notification_test_public_url(settings):
    settings.EVALUATION_PUBLIC_BASE_URL = "https://evaluation.example.test"


@pytest.fixture
def project_with_three_tasks(notification_case):
    return notification_case[0]


@pytest.fixture
def evaluator(notification_case):
    return notification_case[1]


@pytest.fixture
def active_project(project_with_three_tasks):
    project_with_three_tasks.status = EvaluationProject.Status.ACTIVE
    project_with_three_tasks.launched_at = timezone.now()
    project_with_three_tasks.save(update_fields=["status", "launched_at"])
    return project_with_three_tasks


@pytest.fixture
def fake_wecom():
    return FakeWeComNotifier()


@pytest.fixture
def failing_wecom():
    return FakeWeComNotifier(failure_code="WECOM_TIMEOUT")
