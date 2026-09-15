from datetime import timedelta
from copy import deepcopy
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import resolve, reverse
from django.utils import timezone

from apps.evaluations.models import EvaluationProject, EvaluationTask, ProjectSubject
from apps.evaluations.querysets import tasks_for_user
from apps.evaluations.services.templates import create_template_version
from apps.evaluations.services.projects import prepare_project
from apps.evaluations.views import evaluator
from apps.roster.models import Employee
from tests.factories import create_employee, create_project, create_relationship


@pytest.mark.parametrize(
    ("name", "kwargs", "expected_path"),
    [
        ("task_list", {}, "/tasks/"),
        (
            "task_detail",
            {"public_id": "11111111-1111-4111-8111-111111111111"},
            "/tasks/11111111-1111-4111-8111-111111111111/",
        ),
        ("project-create", {}, "/hr/projects/new/"),
        (
            "project-preview",
            {"project_id": "22222222-2222-4222-8222-222222222222"},
            "/hr/projects/22222222-2222-4222-8222-222222222222/preview/",
        ),
        (
            "project-prepare",
            {"project_id": "22222222-2222-4222-8222-222222222222"},
            "/hr/projects/22222222-2222-4222-8222-222222222222/prepare/",
        ),
        (
            "project-detail",
            {"project_id": "22222222-2222-4222-8222-222222222222"},
            "/hr/projects/22222222-2222-4222-8222-222222222222/",
        ),
        (
            "project-extend-deadline",
            {"project_id": "22222222-2222-4222-8222-222222222222"},
            "/hr/projects/22222222-2222-4222-8222-222222222222/extend-deadline/",
        ),
        (
            "project-close",
            {"project_id": "22222222-2222-4222-8222-222222222222"},
            "/hr/projects/22222222-2222-4222-8222-222222222222/close/",
        ),
        ("template-list", {}, "/hr/templates/"),
        ("template-create", {}, "/hr/templates/new/"),
        (
            "template-edit",
            {"template_id": "33333333-3333-4333-8333-333333333333"},
            "/hr/templates/33333333-3333-4333-8333-333333333333/edit/",
        ),
    ],
)
def test_evaluation_routes_keep_literal_hr_prefix_and_root_task_paths(
    name, kwargs, expected_path
):
    qualified_name = f"evaluations:{name}"

    assert reverse(qualified_name, kwargs=kwargs) == expected_path
    assert resolve(expected_path).view_name == qualified_name


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "expected_path"),
    [
        ("task_list", "/tasks/"),
        ("task_detail", "/tasks/11111111-1111-4111-8111-111111111111/"),
        ("project-create", "/hr/projects/new/"),
        (
            "project-preview",
            "/hr/projects/22222222-2222-4222-8222-222222222222/preview/",
        ),
        (
            "project-prepare",
            "/hr/projects/22222222-2222-4222-8222-222222222222/prepare/",
        ),
        (
            "project-detail",
            "/hr/projects/22222222-2222-4222-8222-222222222222/",
        ),
        (
            "project-extend-deadline",
            "/hr/projects/22222222-2222-4222-8222-222222222222/extend-deadline/",
        ),
        (
            "project-close",
            "/hr/projects/22222222-2222-4222-8222-222222222222/close/",
        ),
        ("template-list", "/hr/templates/"),
        ("template-create", "/hr/templates/new/"),
        (
            "template-edit",
            "/hr/templates/33333333-3333-4333-8333-333333333333/edit/",
        ),
    ],
)
def test_each_literal_evaluation_route_has_unauthenticated_client_boundary(
    client, name, expected_path
):
    response = client.get(expected_path)

    assert resolve(expected_path).view_name == f"evaluations:{name}"
    assert response.status_code == 302
    assert response.url == f"/auth/wecom/start/?next={expected_path}"


def _create_second_owned_task(own_task, hr_admin):
    template = own_task.project_subject.template
    subject = create_employee(
        "TASK-SUBJECT-SECOND", "Second Task Subject", category=own_task.subject.category
    )
    peer = own_task.project.tasks.get(
        subject=own_task.subject, relationship_type="same_department"
    ).evaluator
    cross = own_task.project.tasks.get(
        subject=own_task.subject, relationship_type="cross_department"
    ).evaluator
    create_relationship(
        subject=subject, evaluator=own_task.evaluator, relationship_type="manager"
    )
    create_relationship(subject=subject, evaluator=peer, relationship_type="same_department")
    create_relationship(subject=subject, evaluator=cross, relationship_type="cross_department")
    project = create_project(
        name="Second Task Center Project", subject_templates=((subject, template),)
    )
    prepare_project(project, hr_admin)
    project.status = EvaluationProject.Status.ACTIVE
    project.launched_at = project.prepared_at
    project.save(update_fields=["status", "launched_at"])
    return project.tasks.get(evaluator=own_task.evaluator, subject=subject)


@pytest.mark.django_db
def test_task_detail_renders_only_the_frozen_template_snapshot(
    client, evaluator_user, own_task, hr_admin
):
    project_subject = own_task.project_subject
    frozen_title = project_subject.template_snapshot["items"][0]["title"]
    changed_items = project_subject.template.item_payloads()
    changed_items[0]["title"] = "Changed live template item"
    changed_template = create_template_version(
        project_subject.template,
        changes={"name": "Changed live template", "items": changed_items},
        actor=hr_admin,
    )
    ProjectSubject.objects.filter(pk=project_subject.pk).update(template=changed_template)
    client.force_login(evaluator_user)

    response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert frozen_title in content
    assert "Changed live template item" not in content
    assert 'data-evaluation-item' in content


def _not_a_mapping(_snapshot):
    return ["PRIVATE-SNAPSHOT-PAYLOAD"]


def _missing_items(snapshot):
    snapshot.pop("items")
    return snapshot


def _boolean_score_bound(snapshot):
    snapshot["items"][0]["score_min"] = True
    return snapshot


def _inverted_score_bounds(snapshot):
    snapshot["items"][0]["score_min"] = 5
    snapshot["items"][0]["score_max"] = 1
    return snapshot


def _large_score_bound(snapshot):
    snapshot["items"][0]["score_max"] = 999999
    return snapshot


def _invalid_item_weight(snapshot):
    snapshot["items"][0]["weight"] = "PRIVATE-INVALID-WEIGHT"
    return snapshot


def _duplicate_item_order(snapshot):
    snapshot["items"][1]["order"] = snapshot["items"][0]["order"]
    return snapshot


def _missing_item_title(snapshot):
    snapshot["items"][0].pop("title")
    return snapshot


def _invalid_item_shape(snapshot):
    snapshot["items"][0] = {"title": "PRIVATE-TITLE"}
    return snapshot


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (_not_a_mapping, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_missing_items, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_boolean_score_bound, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_inverted_score_bounds, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_large_score_bound, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_invalid_item_weight, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_duplicate_item_order, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_missing_item_title, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
        (_invalid_item_shape, "FROZEN_TEMPLATE_SNAPSHOT_INVALID"),
    ],
)
def test_malformed_frozen_snapshot_returns_generic_non_leaking_error(
    client, caplog, evaluator_user, own_task, mutate, error_code
):
    snapshot = mutate(deepcopy(own_task.project_subject.template_snapshot))
    ProjectSubject.objects.filter(pk=own_task.project_subject_id).update(
        template_snapshot=snapshot
    )
    client.force_login(evaluator_user)
    client.raise_request_exception = False
    caplog.set_level(logging.WARNING, logger="apps.evaluations.views.evaluator")

    response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    content = response.content.decode()
    assert response.status_code == 409
    assert "评价任务暂时无法显示" in content
    assert "PRIVATE-SNAPSHOT-PAYLOAD" not in content
    assert "PRIVATE-TITLE" not in content
    assert "PRIVATE-INVALID-WEIGHT" not in content
    assert str(own_task.public_id) not in content
    assert str(own_task.project.public_id) not in content
    assert any(error_code in record.getMessage() for record in caplog.records)
    assert all(
        "PRIVATE-SNAPSHOT-PAYLOAD" not in record.getMessage()
        and "PRIVATE-TITLE" not in record.getMessage()
        and "PRIVATE-INVALID-WEIGHT" not in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.django_db
def test_task_list_groups_the_same_status_inside_each_project(
    client, evaluator_user, own_task, hr_admin
):
    second_task = _create_second_owned_task(own_task, hr_admin)
    client.force_login(evaluator_user)
    list_url = reverse("evaluations:task_list")

    response = client.get(list_url)
    project_groups = response.context["project_groups"]
    content = response.content.decode()

    assert response.status_code == 200
    assert [group.project.pk for group in project_groups] == [
        own_task.project_id,
        second_task.project_id,
    ]
    assert all(group.buckets["pending"] for group in project_groups)
    assert content.count("待填写") == 2
    assert own_task.project.name in content
    assert second_task.project.name in content
    assert "截止时间" in content


@pytest.mark.django_db
def test_task_list_captures_now_once_for_all_project_groups(
    client, evaluator_user, own_task, hr_admin, monkeypatch
):
    second_task = _create_second_owned_task(own_task, hr_admin)
    boundary = timezone.now()
    for task in (own_task, second_task):
        task.project.deadline = boundary + timedelta(microseconds=1)
        task.project.save(update_fields=["deadline"])
    client.force_login(evaluator_user)
    calls = iter((boundary, boundary + timedelta(microseconds=2)))
    monkeypatch.setattr(evaluator, "timezone", SimpleNamespace(now=lambda: next(calls)))

    response = client.get(reverse("evaluations:task_list"))

    assert response.status_code == 200
    pending_ids = {
        task.pk
        for group in response.context["project_groups"]
        for task in group.buckets["pending"]
    }
    assert {own_task.pk, second_task.pk} <= pending_ids


@pytest.mark.django_db
@pytest.mark.parametrize("closed", [False, True])
def test_expired_or_closed_task_form_is_read_only(
    client, evaluator_user, own_task, closed
):
    if closed:
        own_task.project.status = EvaluationProject.Status.CLOSED
    else:
        own_task.project.deadline = timezone.now() - timedelta(seconds=1)
    own_task.project.save(update_fields=["deadline", "status"] if closed else ["deadline"])
    client.force_login(evaluator_user)

    response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert 'disabled' in content
    assert '只读' in content


@pytest.mark.django_db
def test_unauthenticated_task_requests_redirect_to_wecom_login(client, own_task):
    list_response = client.get(reverse("evaluations:task_list"))
    detail_response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    assert list_response.status_code == detail_response.status_code == 302
    assert list_response.url == "/auth/wecom/start/?next=/tasks/"
    assert detail_response.url == f"/auth/wecom/start/?next=/tasks/{own_task.public_id}/"


@pytest.mark.django_db
def test_unmapped_or_inactive_evaluator_cannot_disclose_tasks(
    client, evaluator_user, own_task
):
    task_url = reverse("evaluations:task_detail", args=[own_task.public_id])
    unmapped = get_user_model().objects.create_user(username="unmapped@example.test")
    client.force_login(unmapped)
    assert client.get(reverse("evaluations:task_list")).status_code == 403
    assert client.get(task_url).status_code == 404

    own_task.evaluator.is_active = False
    own_task.evaluator.save(update_fields=["is_active"])
    client.force_login(evaluator_user)
    assert client.get(reverse("evaluations:task_list")).status_code == 403
    assert client.get(task_url).status_code == 404


@pytest.mark.django_db
def test_nonexistent_and_forwarded_task_urls_are_indistinguishable(
    client, evaluator_user, own_task, other_task
):
    client.force_login(evaluator_user)
    forwarded = client.get(
        reverse("evaluations:task_detail", args=[other_task.public_id])
    )
    nonexistent = client.get(
        reverse("evaluations:task_detail", args=[uuid4()])
    )

    assert forwarded.status_code == nonexistent.status_code == 404
    assert forwarded.content == nonexistent.content


@pytest.mark.django_db
def test_tasks_for_user_keeps_one_query_for_four_hundred_tasks(
    evaluator_user, own_task, django_assert_max_num_queries
):
    project = EvaluationProject.objects.create(
        name="Large fictional task center project",
        status=EvaluationProject.Status.ACTIVE,
        deadline=timezone.now() + timedelta(days=1),
    )
    category = own_task.subject.category
    subjects = [
        Employee(
            employee_no=f"LOAD-{index:03d}",
            name=f"Load Subject {index:03d}",
            corporate_email=f"load-{index:03d}@example.test",
            department_level_1="Load Center",
            department_level_2="Load Team",
            category=category,
        )
        for index in range(400)
    ]
    Employee.objects.bulk_create(subjects)
    project_subjects = [
        ProjectSubject(project=project, subject=subject, template=own_task.project_subject.template)
        for subject in subjects
    ]
    ProjectSubject.objects.bulk_create(project_subjects)
    EvaluationTask.objects.bulk_create(
        [
            EvaluationTask(
                project=project,
                project_subject=project_subject,
                evaluator=own_task.evaluator,
                subject=subject,
                relationship_type="manager",
            )
            for subject, project_subject in zip(subjects, project_subjects, strict=True)
        ]
    )

    with django_assert_max_num_queries(1):
        tasks = list(tasks_for_user(evaluator_user))

    assert len(tasks) == 401


@pytest.mark.django_db
def test_task_html_does_not_expose_sensitive_assignment_or_aggregate_data(
    client, evaluator_user, own_task
):
    client.force_login(evaluator_user)

    response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    content = response.content.decode()
    assert own_task.evaluator.name not in content
    assert own_task.evaluator.employee_no not in content
    assert own_task.evaluator.corporate_email not in content
    assert "汇总" not in content
    assert "relationship_type" not in content
