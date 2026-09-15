from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    models,
    transaction,
)
from django.db.models.query import QuerySet

from apps.evaluations.models import EvaluationProject, EvaluationTask
from apps.roster import imports as roster_imports
from apps.roster import locking as roster_locking
from apps.roster.imports import commit_import_batch, preview_roster_upload
from apps.roster.models import Employee, EmployeeCategory, EvaluationRelationship
from apps.roster.services import RosterValidationError, upsert_relationship
from apps.evaluations.services import projects as project_services
from apps.evaluations.services.projects import ProjectStateError, prepare_project
from tests.factories import create_project, create_relationship
from tests.helpers import ROSTER_HEADERS, build_csv_bytes


@pytest.mark.django_db
def test_prepare_generates_one_unique_task_per_frozen_relationship(draft_project, hr_admin):
    result = prepare_project(draft_project, hr_admin)

    tasks = list(draft_project.tasks.order_by("relationship_type"))
    assert result.created_count == 3
    assert result.total_count == 3
    assert {task.relationship_type for task in tasks} == {
        "manager",
        "same_department",
        "cross_department",
    }
    assert all(UUID(str(task.public_id)) for task in tasks)
    assert all(task.project_subject_id == draft_project.subjects.get().id for task in tasks)
    assert {str(task.relationship_snapshot_item_id) for task in tasks} == {
        item["snapshot_item_id"]
        for item in draft_project.subjects.get().relationship_snapshot
    }


@pytest.mark.django_db
def test_prepare_is_idempotent_for_stale_project_instances_and_does_not_duplicate_tasks(
    draft_project, hr_admin
):
    stale_copy = type(draft_project).objects.get(pk=draft_project.pk)

    first = prepare_project(draft_project, hr_admin)
    second = prepare_project(stale_copy, hr_admin)

    assert first.created_count == second.total_count == 3
    assert second.created_count == 0
    assert draft_project.tasks.count() == 3


@pytest.mark.django_db
def test_task_database_constraint_rejects_duplicate_project_relationship(
    draft_project, hr_admin
):
    prepare_project(draft_project, hr_admin)
    task = draft_project.tasks.get(relationship_type="manager")

    with pytest.raises(IntegrityError):
        EvaluationTask.objects.create(
            project=task.project,
            project_subject=task.project_subject,
            evaluator=task.evaluator,
            subject=task.subject,
            relationship_type=task.relationship_type,
            relationship_snapshot_item_id=task.relationship_snapshot_item_id,
        )


@pytest.mark.django_db
def test_prepare_integrity_race_becomes_stable_domain_error_and_rolls_back(
    draft_project, hr_admin, monkeypatch
):
    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError("simulated concurrent task insert")

    monkeypatch.setattr(EvaluationTask.objects, "bulk_create", raise_integrity_error)

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)

    assert raised.value.code == "PROJECT_PREPARE_CONFLICT"
    draft_project.refresh_from_db()
    assert draft_project.status == "draft"
    assert draft_project.tasks.count() == 0
    assert draft_project.subjects.get().template_snapshot == {}


@pytest.mark.django_db
def test_prepare_integrity_error_leaves_outer_transaction_usable(
    draft_project, hr_admin, monkeypatch
):
    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError("simulated concurrent task insert")

    monkeypatch.setattr(EvaluationTask.objects, "bulk_create", raise_integrity_error)

    with transaction.atomic():
        with pytest.raises(ProjectStateError) as raised:
            prepare_project(draft_project, hr_admin)
        assert raised.value.code == "PROJECT_PREPARE_CONFLICT"
        assert EvaluationProject.objects.filter(pk=draft_project.pk).exists()
        EvaluationProject.objects.create(
            name="Outer transaction remains usable",
            deadline=draft_project.deadline,
        )


@pytest.mark.django_db
def test_prepare_uses_explicit_global_lock_order_without_template_locks(
    draft_project, hr_admin, monkeypatch
):
    from apps.audit.models import AuditLog

    lock_calls = []
    evaluated_shared_locks = []
    original_select_for_update = QuerySet.select_for_update
    original_fetch_all = QuerySet._fetch_all

    def record_select_for_update(queryset, *args, **kwargs):
        lock_calls.append((queryset.model._meta.label_lower, kwargs.get("of")))
        return original_select_for_update(queryset, *args, **kwargs)

    def record_fetch_all(queryset):
        if (
            queryset._result_cache is None
            and queryset.query.select_for_update
            and queryset.model
            in (Employee, EmployeeCategory, EvaluationRelationship)
        ):
            evaluated_shared_locks.append(
                (
                    queryset.model._meta.label_lower,
                    queryset.query.select_for_update_of,
                    queryset.query.order_by,
                )
            )
        return original_fetch_all(queryset)

    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    monkeypatch.setattr(QuerySet, "_fetch_all", record_fetch_all)

    prepare_project(draft_project, hr_admin)

    assert lock_calls == [
        ("evaluations.evaluationproject", ("self",)),
        ("evaluations.projectsubject", ("self",)),
        ("roster.employee", ("self",)),
        ("roster.employeecategory", ("self",)),
        ("roster.evaluationrelationship", ("self",)),
        (AuditLog._meta.label_lower, None),
    ]
    assert evaluated_shared_locks == [
        ("roster.employee", ("self",), ("pk",)),
        ("roster.employeecategory", ("self",), ("pk",)),
        ("roster.evaluationrelationship", ("self",), ("pk",)),
    ]


def _raise_project_lock_error(monkeypatch, error):
    original_get = QuerySet.get

    def raise_project_lock_error(queryset, *args, **kwargs):
        if queryset.model is EvaluationProject and queryset.query.select_for_update:
            raise error
        return original_get(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get", raise_project_lock_error)


def _operational_error_with_driver_code(attribute, code):
    driver_error = RuntimeError("database driver failure")
    setattr(driver_error, attribute, code)
    error = OperationalError("database operation failed")
    error.__cause__ = driver_error
    return error


@pytest.mark.django_db
@pytest.mark.parametrize("sqlstate", ["40P01", "55P03", "40001"])
def test_prepare_maps_only_retryable_postgresql_sqlstates(
    draft_project, hr_admin, monkeypatch, sqlstate
):
    _raise_project_lock_error(
        monkeypatch,
        _operational_error_with_driver_code("sqlstate", sqlstate),
    )

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)

    assert raised.value.code == "PROJECT_PREPARE_RETRY"


@pytest.mark.django_db
def test_prepare_uses_pgcode_fallback_for_retryable_postgresql_error(
    draft_project, hr_admin, monkeypatch
):
    _raise_project_lock_error(
        monkeypatch,
        _operational_error_with_driver_code("pgcode", "40P01"),
    )

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)

    assert raised.value.code == "PROJECT_PREPARE_RETRY"


@pytest.mark.skipif(
    connection.vendor != "sqlite",
    reason="SQLite-specific lock message classification",
)
@pytest.mark.django_db
def test_prepare_maps_sqlite_database_locked_to_retryable_error(
    draft_project, hr_admin, monkeypatch
):
    _raise_project_lock_error(monkeypatch, OperationalError("database is locked"))

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)

    assert raised.value.code == "PROJECT_PREPARE_RETRY"


@pytest.mark.django_db
def test_prepare_does_not_mislabel_unknown_operational_error(
    draft_project, hr_admin, monkeypatch
):
    error = OperationalError("connection unexpectedly closed")
    _raise_project_lock_error(monkeypatch, error)

    with pytest.raises(OperationalError) as raised:
        prepare_project(draft_project, hr_admin)

    assert raised.value is error


@pytest.mark.django_db
def test_unknown_operational_error_rolls_back_savepoint_and_outer_transaction_recovers(
    draft_project, hr_admin, monkeypatch
):
    def execute_missing_table(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM task_6_missing_table")

    monkeypatch.setattr(
        EvaluationTask.objects, "bulk_create", execute_missing_table
    )

    with transaction.atomic():
        with pytest.raises(OperationalError):
            prepare_project(draft_project, hr_admin)
        assert EvaluationProject.objects.filter(pk=draft_project.pk).exists()
        EvaluationProject.objects.create(
            name="Outer transaction recovered after database error",
            deadline=draft_project.deadline,
        )


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update concurrency semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_overlapping_projects_prepare_concurrently_without_deadlock(
    draft_project, hr_admin, monkeypatch
):
    first_subject = draft_project.subjects.get()
    manager_relation = EvaluationRelationship.objects.get(
        subject=first_subject.subject, relationship_type="manager"
    )
    peer_relation = EvaluationRelationship.objects.get(
        subject=first_subject.subject, relationship_type="same_department"
    )
    cross_relation = EvaluationRelationship.objects.get(
        subject=first_subject.subject, relationship_type="cross_department"
    )
    second_subject = manager_relation.evaluator
    create_relationship(
        subject=second_subject,
        evaluator=first_subject.subject,
        relationship_type="manager",
    )
    create_relationship(
        subject=second_subject,
        evaluator=peer_relation.evaluator,
        relationship_type="same_department",
    )
    create_relationship(
        subject=second_subject,
        evaluator=cross_relation.evaluator,
        relationship_type="cross_department",
    )
    second_project = create_project(
        name="Overlapping project",
        subject_templates=((second_subject, first_subject.template),),
    )
    project_ids = [draft_project.pk, second_project.pk]
    actor_id = hr_admin.pk
    barrier = Barrier(2)
    original_lock_dependencies = project_services._lock_preflight_dependencies

    def synchronized_lock_dependencies(project_subjects):
        barrier.wait(timeout=10)
        return original_lock_dependencies(project_subjects)

    monkeypatch.setattr(
        project_services,
        "_lock_preflight_dependencies",
        synchronized_lock_dependencies,
    )

    def prepare_in_own_connection(project_id):
        close_old_connections()
        try:
            project = EvaluationProject.objects.get(pk=project_id)
            actor = type(hr_admin).objects.get(pk=actor_id)
            return prepare_project(project, actor)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(prepare_in_own_connection, project_ids))

    assert [result.created_count for result in results] == [3, 3]
    assert EvaluationTask.objects.filter(project_id__in=project_ids).count() == 6


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update concurrency semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_project_prepare_racing_overlapping_roster_import_does_not_deadlock(
    draft_project, hr_admin, monkeypatch
):
    project_subject = draft_project.subjects.select_related(
        "subject__category"
    ).get()
    subject = project_subject.subject
    batch = preview_roster_upload(
        build_csv_bytes(
            ROSTER_HEADERS,
            [
                (
                    subject.employee_no,
                    "Roster Race Updated Subject",
                    subject.corporate_email,
                    subject.department_level_1,
                    subject.department_level_2,
                    subject.category.code,
                    subject.wecom_userid or "",
                )
            ],
        ),
        "project-roster-race.csv",
        hr_admin,
    )
    barrier = Barrier(2)
    original_project_preflight = project_services._lock_preflight_dependencies
    original_roster_preflight = roster_imports._preflight_roster

    def synchronized_project_preflight(project_subjects):
        barrier.wait(timeout=10)
        return original_project_preflight(project_subjects)

    def synchronized_roster_preflight(import_batch, mode):
        barrier.wait(timeout=10)
        return original_roster_preflight(import_batch, mode)

    monkeypatch.setattr(
        project_services,
        "_lock_preflight_dependencies",
        synchronized_project_preflight,
    )
    monkeypatch.setattr(
        roster_imports, "_preflight_roster", synchronized_roster_preflight
    )
    project_id = draft_project.pk
    batch_id = batch.public_id
    actor_id = hr_admin.pk

    def prepare_in_own_connection():
        close_old_connections()
        try:
            project = EvaluationProject.objects.get(pk=project_id)
            actor = type(hr_admin).objects.get(pk=actor_id)
            return prepare_project(project, actor)
        finally:
            close_old_connections()

    def import_in_own_connection():
        close_old_connections()
        try:
            actor = type(hr_admin).objects.get(pk=actor_id)
            return commit_import_batch(
                batch_id,
                mode="append",
                duplicate_policy="update",
                actor=actor,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        prepare_future = executor.submit(prepare_in_own_connection)
        import_future = executor.submit(import_in_own_connection)
        prepare_result = prepare_future.result(timeout=20)
        import_result = import_future.result(timeout=20)

    draft_project.refresh_from_db()
    subject.refresh_from_db()
    batch.refresh_from_db()
    assert prepare_result.created_count == 3
    assert import_result.updated_count == 1
    assert draft_project.status == EvaluationProject.Status.READY
    assert subject.name == "Roster Race Updated Subject"
    assert batch.status == batch.Status.COMMITTED


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update concurrency semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_project_prepare_racing_standalone_relationship_upsert_does_not_deadlock(
    draft_project, hr_admin, monkeypatch
):
    project_subject, inactive = _create_project_upsert_barrier_fixture(draft_project)
    barrier = Barrier(2)
    original_employee_lock = roster_locking.lock_employee_rows

    def synchronized_employee_lock(employee_ids):
        barrier.wait(timeout=10)
        return original_employee_lock(employee_ids)

    monkeypatch.setattr(
        roster_locking, "lock_employee_rows", synchronized_employee_lock
    )
    project_id = draft_project.pk
    actor_id = hr_admin.pk
    subject_no = inactive.subject.employee_no
    evaluator_no = inactive.evaluator.employee_no

    def prepare_in_own_connection():
        close_old_connections()
        try:
            project = EvaluationProject.objects.get(pk=project_id)
            actor = type(hr_admin).objects.get(pk=actor_id)
            return prepare_project(project, actor)
        finally:
            close_old_connections()

    def upsert_in_own_connection():
        close_old_connections()
        try:
            actor = type(hr_admin).objects.get(pk=actor_id)
            return upsert_relationship(
                subject_no=subject_no,
                evaluator_no=evaluator_no,
                relationship_type=EvaluationRelationship.Type.MANAGER,
                actor=actor,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        prepare_future = executor.submit(prepare_in_own_connection)
        upsert_future = executor.submit(upsert_in_own_connection)
        prepare_result = prepare_future.result(timeout=20)
        with pytest.raises(RosterValidationError) as raised:
            upsert_future.result(timeout=20)

    draft_project.refresh_from_db()
    inactive.refresh_from_db()
    assert prepare_result.created_count == 3
    assert raised.value.code == "DUPLICATE_RELATION"
    assert draft_project.status == EvaluationProject.Status.READY
    assert inactive.is_active is False
    assert EvaluationRelationship.objects.filter(
        subject=project_subject.subject,
        evaluator=inactive.evaluator,
        relationship_type=inactive.relationship_type,
        is_active=True,
    ).count() == 1


def _create_project_upsert_barrier_fixture(draft_project):
    project_subject = draft_project.subjects.get()
    manager = EvaluationRelationship.objects.get(
        subject=project_subject.subject,
        relationship_type=EvaluationRelationship.Type.MANAGER,
    ).evaluator
    inactive = EvaluationRelationship.objects.create(
        subject=project_subject.subject,
        evaluator=manager,
        relationship_type=EvaluationRelationship.Type.MANAGER,
        is_active=False,
    )
    return project_subject, inactive


@pytest.mark.django_db
def test_project_upsert_barrier_fixture_overlaps_project_relationship_locks(
    draft_project, hr_admin, monkeypatch
):
    project_subject, inactive = _create_project_upsert_barrier_fixture(draft_project)
    locked_relationship_ids = []
    original_fetch_all = QuerySet._fetch_all

    def record_locked_relationships(queryset):
        original_fetch_all(queryset)
        if (
            queryset.model is EvaluationRelationship
            and queryset.query.select_for_update
        ):
            locked_relationship_ids.extend(
                relationship.pk for relationship in queryset._result_cache
            )

    monkeypatch.setattr(QuerySet, "_fetch_all", record_locked_relationships)

    prepare_project(draft_project, hr_admin)

    project_subject_ids = {project_subject.subject_id}
    relationship_metadata = list(
        EvaluationRelationship.objects.filter(subject_id__in=project_subject_ids)
        .order_by("pk")
        .values_list("pk", "evaluator_id")
    )
    project_employee_ids = project_subject_ids | {
        evaluator_id for _, evaluator_id in relationship_metadata
    }
    upsert_employee_ids = {inactive.subject_id, inactive.evaluator_id}

    assert upsert_employee_ids <= project_employee_ids
    assert inactive.pk in {relation_id for relation_id, _ in relationship_metadata}
    assert inactive.pk in locked_relationship_ids


def _valid_task_values(draft_project):
    project_subject = draft_project.subjects.get()
    relation = EvaluationRelationship.objects.filter(
        subject=project_subject.subject
    ).order_by("pk").first()
    return {
        "project": draft_project,
        "project_subject": project_subject,
        "evaluator": relation.evaluator,
        "subject": project_subject.subject,
        "relationship_type": relation.relationship_type,
    }


@pytest.mark.django_db
def test_task_instance_save_rejects_project_subject_project_mismatch(draft_project):
    values = _valid_task_values(draft_project)
    values["project"] = create_project(name="Contradictory project")
    task = EvaluationTask(**values)

    with pytest.raises(ValidationError, match="任务项目关联不一致"):
        task.save()

    assert EvaluationTask.objects.count() == 0


@pytest.mark.django_db
def test_task_manager_create_rejects_project_subject_subject_mismatch(draft_project):
    values = _valid_task_values(draft_project)
    values["subject"] = values["evaluator"]

    with pytest.raises(ValidationError, match="任务被评价人关联不一致"):
        EvaluationTask.objects.create(**values)

    assert EvaluationTask.objects.count() == 0


@pytest.mark.django_db
def test_task_bulk_create_rejects_inconsistent_associations(draft_project):
    values = _valid_task_values(draft_project)
    values["project"] = create_project(name="Bulk contradictory project")

    with pytest.raises(ValidationError, match="任务项目关联不一致"):
        EvaluationTask.objects.bulk_create([EvaluationTask(**values)])

    assert EvaluationTask.objects.count() == 0


@pytest.mark.django_db
def test_task_bulk_update_rejects_inconsistent_associations(draft_project, hr_admin):
    prepare_project(draft_project, hr_admin)
    task = draft_project.tasks.order_by("pk").first()
    original_subject_id = task.subject_id
    task.subject = task.evaluator

    with pytest.raises(ValidationError, match="任务被评价人关联不一致"):
        EvaluationTask.objects.bulk_update([task], ["subject"])

    task.refresh_from_db()
    assert task.subject_id == original_subject_id


@pytest.mark.django_db
def test_task_queryset_cannot_rewrite_canonical_associations(draft_project, hr_admin):
    prepare_project(draft_project, hr_admin)
    other_project = create_project(name="Queryset contradictory project")

    with pytest.raises(ValidationError, match="任务关联字段"):
        draft_project.tasks.update(project=other_project)

    assert draft_project.tasks.count() == 3
    assert EvaluationTask.objects.filter(project=other_project).count() == 0


@pytest.mark.django_db
def test_task_relationship_type_has_choices_and_model_validation(draft_project):
    field = EvaluationTask._meta.get_field("relationship_type")
    values = _valid_task_values(draft_project)
    values["relationship_type"] = "invalid_type"

    assert dict(field.choices) == dict(EvaluationRelationship.Type.choices)
    with pytest.raises(ValidationError, match="任务关系类型无效"):
        EvaluationTask.objects.create(**values)


@pytest.mark.django_db(transaction=True)
def test_task_relationship_type_database_constraint_rejects_bypass(draft_project):
    task = EvaluationTask(**_valid_task_values(draft_project))
    task.relationship_type = "invalid_type"

    with pytest.raises(IntegrityError):
        models.Model.save(task, force_insert=True)
