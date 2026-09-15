from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from itertools import combinations
from io import StringIO
from threading import Event, Lock
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.evaluations.management.commands import recompute_project
from apps.evaluations.models import (
    AggregateResult,
    EvaluationProject,
    EvaluationTask,
    ProjectSubject,
    Submission,
    SubmissionAnswer,
)
from apps.evaluations.services import submissions as submission_service
from apps.evaluations.services.projects import prepare_project
from apps.evaluations.services.scoring import (
    ALGORITHM_VERSION,
    calculate_project_results,
    calculate_subject_result,
)
from apps.evaluations.services.submissions import submit_task
from apps.roster.models import Employee
from tests.factories import (
    create_category,
    create_employee,
    create_project,
    create_relationship,
    create_template,
)


pytestmark = pytest.mark.django_db


def _result_values(outcome_data):
    project_subject = outcome_data["project"].subjects.get(
        subject=outcome_data["subject"]
    )
    return {
        "project": outcome_data["project"],
        "subject": outcome_data["subject"],
        "project_subject": project_subject,
        "frozen_subject_public_id": UUID(
            project_subject.subject_snapshot["public_id"]
        ),
        "frozen_subject_name": project_subject.subject_snapshot["name"],
        "manager_score": "4.00",
        "same_department_score": "3.50",
        "cross_department_score": "3.00",
        "total_score": "3.65",
        "status": "complete",
        "missing_groups": [],
        "valid_submission_count": 4,
        "algorithm_version": ALGORITHM_VERSION,
        "calculated_at": timezone.now(),
        "integrity_status": "verified",
        "input_fingerprint": "a" * 64,
    }


def test_aggregate_result_has_unguessable_identity_and_auditable_fields(
    complete_subject_result_data,
):
    result = AggregateResult.objects.create(
        **_result_values(complete_subject_result_data)
    )

    assert isinstance(result.public_id, UUID)
    project_subject = complete_subject_result_data["project"].subjects.get()
    assert result.project_subject == project_subject
    assert (
        result.frozen_subject_public_id
        == complete_subject_result_data["subject"].public_id
    )
    assert result.frozen_subject_name == project_subject.subject_snapshot["name"]
    assert result.integrity_status == "verified"
    assert result.input_fingerprint == "a" * 64


def test_database_enforces_one_result_per_project_subject_algorithm(
    complete_subject_result_data,
):
    values = _result_values(complete_subject_result_data)
    AggregateResult.objects.create(**values)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AggregateResult.objects.create(**values)


@pytest.mark.parametrize(
    ("status", "total_score"),
    (("complete", None), ("incomplete", "3.65")),
)
def test_database_rejects_status_total_inconsistency(
    complete_subject_result_data, status, total_score
):
    values = _result_values(complete_subject_result_data)
    values.update(status=status, total_score=total_score)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AggregateResult.objects.create(**values)


@pytest.mark.parametrize(
    "changes",
    (
        {"manager_score": None},
        {"total_score": "5.01"},
        {
            "status": "incomplete",
            "total_score": None,
            "missing_groups": ["manager"],
        },
    ),
)
def test_database_rejects_group_or_total_integrity_mismatch(
    complete_subject_result_data, changes
):
    values = _result_values(complete_subject_result_data)
    values.update(changes)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AggregateResult.objects.create(**values)


GROUP_FIELDS = {
    "manager": "manager_score",
    "same_department": "same_department_score",
    "cross_department": "cross_department_score",
}
VALID_MISSING_SUBSETS = tuple(
    subset
    for size in range(1, 4)
    for subset in combinations(GROUP_FIELDS, size)
)


@pytest.mark.parametrize("missing_groups", VALID_MISSING_SUBSETS)
def test_database_accepts_all_seven_ordered_missing_group_subsets(
    complete_subject_result_data, missing_groups
):
    values = _result_values(complete_subject_result_data)
    values.update(status="incomplete", total_score=None)
    for group in missing_groups:
        values[GROUP_FIELDS[group]] = None
    values["missing_groups"] = list(missing_groups)

    result = AggregateResult.objects.create(**values)

    assert result.missing_groups == list(missing_groups)


@pytest.mark.parametrize(
    "changes",
    (
        {"missing_groups": ["manager"]},
        {
            "status": "incomplete",
            "total_score": None,
            "manager_score": None,
            "missing_groups": [],
        },
        {
            "status": "incomplete",
            "total_score": None,
            "manager_score": None,
            "missing_groups": ["same_department"],
        },
        {
            "status": "incomplete",
            "total_score": None,
            "manager_score": None,
            "same_department_score": None,
            "missing_groups": ["same_department", "manager"],
        },
        {
            "status": "incomplete",
            "total_score": None,
            "manager_score": None,
            "missing_groups": ["manager", "unexpected"],
        },
    ),
)
def test_database_rejects_noncanonical_missing_groups(
    complete_subject_result_data, changes
):
    values = _result_values(complete_subject_result_data)
    values.update(changes)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AggregateResult.objects.create(**values)


@pytest.mark.parametrize(
    "changes",
    (
        {"algorithm_version": ""},
        {"algorithm_version": "   "},
        {"input_fingerprint": "A" * 64},
        {"input_fingerprint": "a" * 63},
        {"input_fingerprint": "a" * 63 + "g"},
        {"frozen_subject_name": ""},
        {"frozen_subject_name": "   "},
    ),
)
def test_database_rejects_noncanonical_result_metadata(
    complete_subject_result_data, changes
):
    values = _result_values(complete_subject_result_data)
    values.update(changes)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AggregateResult.objects.create(**values)


def test_identity_migration_rejects_snapshot_employee_uuid_mismatch(
    complete_subject_result_data,
):
    AggregateResult.objects.create(
        **_result_values(complete_subject_result_data)
    )
    project_subject = complete_subject_result_data["project"].subjects.get()
    snapshot = deepcopy(project_subject.subject_snapshot)
    snapshot["public_id"] = str(uuid4())
    project_subject.subject_snapshot = snapshot
    project_subject.save(update_fields=["subject_snapshot"])
    migration = import_module(
        "apps.evaluations.migrations."
        "0007_aggregate_frozen_identity_and_integrity"
    )

    with pytest.raises(RuntimeError, match="invalid frozen subject identity"):
        migration.populate_frozen_subject_identity(
            django_apps,
            SimpleNamespace(connection=connection),
        )


def _migrate_evaluations_to(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


@pytest.mark.django_db(transaction=True)
def test_aggregate_result_migrates_from_0006_to_0007_with_frozen_identity():
    executor = MigrationExecutor(connection)
    current_leaves = executor.loader.graph.leaf_nodes()
    evaluation_leaves = [leaf for leaf in current_leaves if leaf[0] == "evaluations"]
    assert len(evaluation_leaves) == 1
    try:
        legacy_apps = _migrate_evaluations_to(
            ("evaluations", "0006_aggregateresult")
        )

        User = legacy_apps.get_model("accounts", "User")
        EmployeeCategory = legacy_apps.get_model("roster", "EmployeeCategory")
        LegacyEmployee = legacy_apps.get_model("roster", "Employee")
        LegacyTemplate = legacy_apps.get_model("evaluations", "FormTemplate")
        LegacyProject = legacy_apps.get_model("evaluations", "EvaluationProject")
        LegacyProjectSubject = legacy_apps.get_model(
            "evaluations", "ProjectSubject"
        )
        LegacyAggregateResult = legacy_apps.get_model(
            "evaluations", "AggregateResult"
        )
        token = uuid4().hex
        frozen_public_id = uuid4()
        result_public_id = uuid4()
        fingerprint = "1" * 64

        user = User.objects.create(username=f"legacy-{token}@example.test")
        category = EmployeeCategory.objects.create(
            code=f"LEGACY-{token[:8]}", name=f"Legacy Category {token}"
        )
        subject = LegacyEmployee.objects.create(
            public_id=frozen_public_id,
            employee_no=f"LEGACY-{token}",
            name="Frozen Legacy Subject",
            corporate_email=f"legacy-{token}@example.test",
            department_level_1="Legacy Center",
            department_level_2="Legacy Team",
            category_id=category.pk,
        )
        template = LegacyTemplate.objects.create(
            category_id=category.pk,
            name=f"Legacy Template {token}",
            version=1,
            is_sealed=True,
            created_by_id=user.pk,
        )
        project = LegacyProject.objects.create(
            name=f"Legacy Project {token}",
            status="ready",
            deadline=timezone.now() + timedelta(days=1),
            rule_snapshot={
                "manager": "0.50",
                "same_department": "0.30",
                "cross_department": "0.20",
            },
            prepared_at=timezone.now(),
        )
        project_subject = LegacyProjectSubject.objects.create(
            project_id=project.pk,
            subject_id=subject.pk,
            template_id=template.pk,
            subject_snapshot={
                "public_id": str(frozen_public_id),
                "name": "Frozen Legacy Subject",
            },
            template_snapshot={},
            relationship_snapshot=[],
        )
        legacy_result = LegacyAggregateResult.objects.create(
            public_id=result_public_id,
            project_id=project.pk,
            subject_id=subject.pk,
            manager_score=Decimal("4.00"),
            same_department_score=Decimal("3.50"),
            cross_department_score=Decimal("3.00"),
            total_score=Decimal("3.65"),
            status="complete",
            missing_groups=[],
            valid_submission_count=3,
            algorithm_version="legacy-v1",
            calculated_at=timezone.now(),
            integrity_status="verified",
            input_fingerprint=fingerprint,
        )

        migrated_apps = _migrate_evaluations_to(
            ("evaluations", "0007_aggregate_frozen_identity_and_integrity")
        )
        MigratedAggregateResult = migrated_apps.get_model(
            "evaluations", "AggregateResult"
        )
        migrated = MigratedAggregateResult.objects.get(pk=legacy_result.pk)

        assert migrated.public_id == result_public_id
        assert migrated.project_subject_id == project_subject.pk
        assert migrated.frozen_subject_public_id == frozen_public_id
        assert migrated.frozen_subject_name == "Frozen Legacy Subject"
        assert migrated.input_fingerprint == fingerprint
        assert migrated.integrity_status == "verified"
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MigratedAggregateResult.objects.filter(pk=migrated.pk).update(
                    input_fingerprint="A" * 64
                )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MigratedAggregateResult.objects.filter(pk=migrated.pk).update(
                    missing_groups=["manager"]
                )
    finally:
        MigrationExecutor(connection).migrate(current_leaves)


def test_recompute_project_persists_complete_outcome(complete_subject_result_data):
    output = StringIO()

    call_command(
        "recompute_project",
        project=str(complete_subject_result_data["project"].public_id),
        stdout=output,
    )

    result = AggregateResult.objects.get(
        project=complete_subject_result_data["project"],
        subject=complete_subject_result_data["subject"],
        algorithm_version=ALGORITHM_VERSION,
    )
    assert result.manager_score == 4
    assert result.same_department_score == Decimal("3.50")
    assert result.cross_department_score == 3
    assert result.total_score == Decimal("3.65")
    assert result.status == "complete"
    assert result.missing_groups == []
    assert result.valid_submission_count == 4
    project_subject = complete_subject_result_data["project"].subjects.get()
    assert result.project_subject == project_subject
    assert (
        result.frozen_subject_public_id
        == complete_subject_result_data["subject"].public_id
    )
    assert result.frozen_subject_name == project_subject.subject_snapshot["name"]
    assert result.integrity_status == "verified"
    assert len(result.input_fingerprint) == 64
    assert "recomputed=1" in output.getvalue()


def test_recompute_project_persists_incomplete_outcome(incomplete_subject_result_data):
    call_command(
        "recompute_project",
        project=str(incomplete_subject_result_data["project"].public_id),
    )

    result = AggregateResult.objects.get(
        project=incomplete_subject_result_data["project"],
        subject=incomplete_subject_result_data["subject"],
        algorithm_version=ALGORITHM_VERSION,
    )
    assert result.manager_score is None
    assert result.total_score is None
    assert result.status == "incomplete"
    assert result.missing_groups == ["manager"]
    assert result.valid_submission_count == 2


def test_recompute_persists_frozen_name_after_current_employee_changes(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    project_subject = project.subjects.get()
    frozen_name = project_subject.subject_snapshot["name"]
    subject = complete_subject_result_data["subject"]
    subject.name = "Current Name Changed"
    subject.is_active = False
    subject.save(update_fields=["name", "is_active"])

    call_command("recompute_project", project=str(project.public_id))

    result = AggregateResult.objects.get(project_subject=project_subject)
    assert result.frozen_subject_public_id == UUID(
        project_subject.subject_snapshot["public_id"]
    )
    assert result.frozen_subject_name == frozen_name
    assert result.frozen_subject_name != subject.name


def _assert_result_matches_outcome(result, outcome, project_subject):
    assert result.input_fingerprint == outcome.input_fingerprint
    assert result.valid_submission_count == outcome.valid_submission_count
    assert result.manager_score == outcome.group_scores["manager"]
    assert result.same_department_score == outcome.group_scores[
        "same_department"
    ]
    assert result.cross_department_score == outcome.group_scores[
        "cross_department"
    ]
    assert result.total_score == outcome.total_score
    assert result.status == outcome.status
    assert result.missing_groups == list(outcome.missing_groups)
    assert result.integrity_status == AggregateResult.IntegrityStatus.VERIFIED
    assert result.frozen_subject_public_id == outcome.subject_public_id
    assert result.frozen_subject_name == outcome.subject_name
    assert result.project_subject_id == project_subject.pk
    assert result.algorithm_version == ALGORITHM_VERSION


def test_recompute_project_is_idempotent_and_matches_fresh_source(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    subject = complete_subject_result_data["subject"]
    project_subject = project.subjects.get(subject=subject)
    project_id = str(complete_subject_result_data["project"].public_id)
    call_command("recompute_project", project=project_id)
    first = AggregateResult.objects.get()
    first_identity = (first.public_id, first.input_fingerprint)

    call_command("recompute_project", project=project_id)

    persisted = AggregateResult.objects.get()
    fresh = calculate_subject_result(project, subject)
    assert AggregateResult.objects.count() == 1
    assert (persisted.public_id, persisted.input_fingerprint) == first_identity
    _assert_result_matches_outcome(persisted, fresh, project_subject)


def test_recompute_locks_project_then_all_tasks_before_loading_scoring_source(
    complete_subject_result_data, monkeypatch
):
    events = []
    original_project_lock = recompute_project._lock_project
    original_lock = recompute_project._lock_project_tasks
    original_load = recompute_project.load_project_scoring_source

    def observed_project_lock(project_public_id):
        project = original_project_lock(project_public_id)
        events.append(("project_locked", project.pk))
        return project

    def observed_lock(project):
        tasks = original_lock(project)
        assert all(
            "project_subject" not in task._state.fields_cache for task in tasks
        )
        events.append(("tasks_locked", tuple(task.pk for task in tasks)))
        return tasks

    def observed_load(project, *, tasks):
        events.append(("source_loaded", tuple(task.pk for task in tasks)))
        return original_load(project, tasks=tasks)

    monkeypatch.setattr(
        recompute_project, "_lock_project", observed_project_lock
    )
    monkeypatch.setattr(recompute_project, "_lock_project_tasks", observed_lock)
    monkeypatch.setattr(
        recompute_project, "load_project_scoring_source", observed_load
    )

    call_command(
        "recompute_project",
        project=str(complete_subject_result_data["project"].public_id),
    )

    assert [name for name, _ in events] == [
        "project_locked",
        "tasks_locked",
        "source_loaded",
    ]
    assert events[1][1] == tuple(sorted(events[1][1]))
    assert events[2][1] == events[1][1]


def _create_scale_project_draft(hr_admin, *, subject_count):
    token = uuid4().hex[:8]
    category = create_category(f"SCALE-{token}", f"Scale Category {token}")
    template = create_template(category=category, created_by=hr_admin)
    manager = create_employee(
        f"SCALE-M-{token}",
        "Scale Manager",
        category=category,
        wecom_userid=f"wx_scale_m_{token}",
    )
    peer = create_employee(
        f"SCALE-P-{token}",
        "Scale Peer",
        category=category,
        wecom_userid=f"wx_scale_p_{token}",
    )
    cross = create_employee(
        f"SCALE-C-{token}",
        "Scale Cross",
        category=category,
        department_level_1="Other Center",
        department_level_2="Other Team",
        wecom_userid=f"wx_scale_c_{token}",
    )
    subjects = []
    for index in range(subject_count):
        subject = create_employee(
            f"SCALE-S-{token}-{index}",
            f"Scale Subject {index}",
            category=category,
            wecom_userid=f"wx_scale_s_{token}_{index}",
        )
        subjects.append(subject)
        create_relationship(
            subject=subject,
            evaluator=manager,
            relationship_type="manager",
        )
        create_relationship(
            subject=subject,
            evaluator=peer,
            relationship_type="same_department",
        )
        create_relationship(
            subject=subject,
            evaluator=cross,
            relationship_type="cross_department",
        )
    project = create_project(
        subject_templates=tuple((subject, template) for subject in subjects)
    )
    return project


def _add_final_scale_submissions(project):
    project.status = EvaluationProject.Status.ACTIVE
    project.save(update_fields=["status"])
    tasks = list(project.tasks.select_related("project_subject").order_by("pk"))
    now = timezone.now()
    submissions = Submission.objects.bulk_create(
        [
            Submission(task=task, is_final=True, submitted_at=now)
            for task in tasks
        ]
    )
    answers = []
    for task, submission in zip(tasks, submissions, strict=True):
        for item in task.project_subject.template_snapshot["items"]:
            answers.append(
                SubmissionAnswer(
                    submission=submission,
                    item_snapshot_id=item["snapshot_item_id"],
                    score=4,
                )
            )
    SubmissionAnswer.objects.bulk_create(answers)
    EvaluationTask.objects.filter(project=project).update(
        status=EvaluationTask.Status.SUBMITTED
    )


def test_recompute_query_count_is_bounded_for_five_hundred_subjects(hr_admin):
    project = _create_scale_project_draft(hr_admin, subject_count=500)

    prepare_started = perf_counter()
    with CaptureQueriesContext(connection) as prepare_queries:
        prepared = prepare_project(project, hr_admin)
    prepare_seconds = perf_counter() - prepare_started

    assert prepared.created_count == 1500
    assert ProjectSubject.objects.filter(project=project).count() == 500
    assert EvaluationTask.objects.filter(project=project).count() == 1500
    assert len(prepare_queries) <= 50
    _add_final_scale_submissions(project)

    create_started = perf_counter()
    with CaptureQueriesContext(connection) as create_queries:
        call_command(
            "recompute_project",
            project=str(project.public_id),
            stdout=StringIO(),
        )
    create_seconds = perf_counter() - create_started
    public_ids = dict(
        AggregateResult.objects.filter(project=project).values_list(
            "project_subject_id", "public_id"
        )
    )
    update_started = perf_counter()
    with CaptureQueriesContext(connection) as update_queries:
        call_command(
            "recompute_project",
            project=str(project.public_id),
            stdout=StringIO(),
        )
    update_seconds = perf_counter() - update_started
    assert AggregateResult.objects.filter(project=project).count() == 500
    assert len(create_queries) <= 30
    assert len(update_queries) <= 30
    assert dict(
        AggregateResult.objects.filter(project=project).values_list(
            "project_subject_id", "public_id"
        )
    ) == public_ids
    print(
        "500-subject scale: "
        f"prepare={len(prepare_queries)} queries/{prepare_seconds:.3f}s, "
        f"create_recompute={len(create_queries)} queries/{create_seconds:.3f}s, "
        f"update_recompute={len(update_queries)} queries/{update_seconds:.3f}s"
    )


def test_recompute_removes_only_same_algorithm_stale_rows(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    category = complete_subject_result_data["subject"].category
    stale_subject = create_employee(
        "STALE-RESULT", "Stale Result Subject", category=category
    )
    foreign_project = create_project(
        subject_templates=(
            (
                stale_subject,
                complete_subject_result_data["project"].subjects.get().template,
            ),
        )
    )
    foreign_project_subject = foreign_project.subjects.get()
    stale_values = {
        **_result_values(complete_subject_result_data),
        "subject": stale_subject,
        "project_subject": foreign_project_subject,
        "frozen_subject_public_id": stale_subject.public_id,
        "frozen_subject_name": "Frozen Stale Result Subject",
    }
    current_stale = AggregateResult.objects.create(
        **stale_values
    )
    old_algorithm = AggregateResult.objects.create(
        **{
            **stale_values,
            "algorithm_version": "legacy-v0",
        }
    )

    call_command("recompute_project", project=str(project.public_id))

    assert not AggregateResult.objects.filter(pk=current_stale.pk).exists()
    assert AggregateResult.objects.filter(pk=old_algorithm.pk).exists()


def test_recompute_failure_rolls_back_existing_result(complete_subject_result_data):
    project = complete_subject_result_data["project"]
    call_command("recompute_project", project=str(project.public_id))
    before = AggregateResult.objects.get()
    before_values = (
        before.public_id,
        before.calculated_at,
        before.input_fingerprint,
    )
    project_subject = project.subjects.get()
    project_subject.template_snapshot = {"corrupt": True}
    project_subject.save(update_fields=["template_snapshot"])

    with pytest.raises(
        CommandError, match="SCORING_FAILED:PROJECT_FROZEN_DATA_INVALID"
    ):
        call_command("recompute_project", project=str(project.public_id))

    after = AggregateResult.objects.get()
    assert (
        after.public_id,
        after.calculated_at,
        after.input_fingerprint,
    ) == before_values


def test_recompute_validates_every_subject_before_any_result_write(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    first_project_subject = project.subjects.get()
    second_subject = create_employee(
        "ROLLBACK-SUBJECT",
        "Rollback Subject",
        category=complete_subject_result_data["subject"].category,
    )
    second_subject_snapshot = deepcopy(first_project_subject.subject_snapshot)
    second_subject_snapshot.update(
        public_id=str(second_subject.public_id), name=second_subject.name
    )
    second_project_subject = ProjectSubject.objects.create(
        project=project,
        subject=second_subject,
        template=first_project_subject.template,
        subject_snapshot=second_subject_snapshot,
        template_snapshot=deepcopy(first_project_subject.template_snapshot),
        relationship_snapshot=deepcopy(first_project_subject.relationship_snapshot),
    )
    for source_task in project.tasks.filter(
        project_subject=first_project_subject
    ).order_by("pk"):
        EvaluationTask.objects.create(
            project=project,
            project_subject=second_project_subject,
            evaluator=source_task.evaluator,
            subject=second_subject,
            relationship_type=source_task.relationship_type,
            relationship_snapshot_item_id=source_task.relationship_snapshot_item_id,
        )
    second_project_subject.template_snapshot = {"corrupt": True}
    second_project_subject.save(update_fields=["template_snapshot"])

    with pytest.raises(
        CommandError, match="SCORING_FAILED:PROJECT_FROZEN_DATA_INVALID"
    ):
        call_command("recompute_project", project=str(project.public_id))

    assert AggregateResult.objects.filter(project=project).count() == 0


def test_recompute_never_modifies_tasks_submissions_or_answers(
    complete_subject_result_data,
):
    project = complete_subject_result_data["project"]
    task_rows = list(project.tasks.values_list("pk", "status"))
    submission_rows = list(
        Submission.objects.filter(task__project=project).values_list(
            "pk", "is_final", "version", "submitted_at"
        )
    )
    answer_rows = list(
        Submission.objects.filter(task__project=project)
        .order_by("pk", "answers__pk")
        .values_list("answers__pk", "answers__item_snapshot_id", "answers__score")
    )

    call_command("recompute_project", project=str(project.public_id))

    assert list(project.tasks.values_list("pk", "status")) == task_rows
    assert list(
        Submission.objects.filter(task__project=project).values_list(
            "pk", "is_final", "version", "submitted_at"
        )
    ) == submission_rows
    assert list(
        Submission.objects.filter(task__project=project)
        .order_by("pk", "answers__pk")
        .values_list("answers__pk", "answers__item_snapshot_id", "answers__score")
    ) == answer_rows


@pytest.mark.parametrize(
    ("project_value", "error_code"),
    (
        ("not-a-uuid", "PROJECT_ID_INVALID"),
        ("00000000-0000-0000-0000-000000000000", "PROJECT_NOT_FOUND"),
    ),
)
def test_recompute_rejects_malformed_or_unknown_project(project_value, error_code):
    with pytest.raises(CommandError, match=error_code):
        call_command("recompute_project", project=project_value)


def test_recompute_rejects_unprepared_project(hr_admin):
    project = create_project(status=EvaluationProject.Status.DRAFT)

    with pytest.raises(CommandError, match="PROJECT_NOT_READY"):
        call_command("recompute_project", project=str(project.public_id))


def _concurrent_recompute(project_id, started=None, done=None):
    close_old_connections()
    try:
        if started is not None:
            started.set()
        output = StringIO()
        call_command("recompute_project", project=project_id, stdout=output)
        return output.getvalue()
    finally:
        if done is not None:
            done.set()
        close_old_connections()


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="select_for_update concurrency semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_recompute_proves_real_lock_overlap(
    complete_subject_result_data, monkeypatch
):
    project_id = str(complete_subject_result_data["project"].public_id)
    first_tasks_locked = Event()
    release_first = Event()
    second_started = Event()
    second_project_lock_attempted = Event()
    second_tasks_locked = Event()
    release_second = Event()
    second_done = Event()
    guard = Lock()
    project_lock_calls = 0
    task_lock_calls = 0
    captured_outcomes = []
    original_project_lock = recompute_project._lock_project
    original_task_lock = recompute_project._lock_project_tasks
    original_calculate = recompute_project.calculate_project_results

    def observe_project_lock_attempt(project_public_id):
        nonlocal project_lock_calls
        with guard:
            project_lock_calls += 1
            call_number = project_lock_calls
        if call_number == 2:
            second_project_lock_attempted.set()
        return original_project_lock(project_public_id)

    def pause_each_after_real_task_lock(project):
        nonlocal task_lock_calls
        tasks = original_task_lock(project)
        with guard:
            task_lock_calls += 1
            call_number = task_lock_calls
        if call_number == 1:
            first_tasks_locked.set()
            assert release_first.wait(timeout=10)
        else:
            second_tasks_locked.set()
            assert release_second.wait(timeout=10)
        return tasks

    def capture_actual_outcomes(source):
        outcomes = original_calculate(source)
        with guard:
            captured_outcomes.append(outcomes)
        return outcomes

    monkeypatch.setattr(
        recompute_project,
        "_lock_project",
        observe_project_lock_attempt,
    )
    monkeypatch.setattr(
        recompute_project,
        "_lock_project_tasks",
        pause_each_after_real_task_lock,
    )
    monkeypatch.setattr(
        recompute_project,
        "calculate_project_results",
        capture_actual_outcomes,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_concurrent_recompute, project_id)
        assert first_tasks_locked.wait(timeout=10)
        second = pool.submit(
            _concurrent_recompute,
            project_id,
            second_started,
            second_done,
        )
        assert second_started.wait(timeout=10)
        assert second_project_lock_attempted.wait(timeout=10)
        assert not second_tasks_locked.wait(timeout=0.5)
        assert not second_done.is_set()
        release_first.set()
        first_output = first.result(timeout=20)
        assert second_tasks_locked.wait(timeout=10)
        first_persisted = AggregateResult.objects.get(
            project=complete_subject_result_data["project"],
            algorithm_version=ALGORITHM_VERSION,
        )
        first_public_id = first_persisted.public_id
        release_second.set()
        second_output = second.result(timeout=20)

    persisted = AggregateResult.objects.get(
        project=complete_subject_result_data["project"],
        algorithm_version=ALGORITHM_VERSION,
    )
    project_subject = complete_subject_result_data["project"].subjects.get()
    fresh = calculate_subject_result(
        complete_subject_result_data["project"],
        complete_subject_result_data["subject"],
    )
    assert "recomputed=1" in first_output
    assert "recomputed=1" in second_output
    assert len(captured_outcomes) == 2
    for outcomes in captured_outcomes:
        assert len(outcomes) == 1
        _, outcome = outcomes[0]
        assert outcome == fresh
    assert persisted.public_id == first_public_id
    _assert_result_matches_outcome(persisted, fresh, project_subject)


def _concurrent_submit(task_id, user_id, answers, started, done):
    close_old_connections()
    try:
        task = EvaluationTask.objects.get(pk=task_id)
        user = get_user_model().objects.get(pk=user_id)
        started.set()
        submit_task(task, user, answers, "recompute-race-submit")
    finally:
        done.set()
        close_old_connections()


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="recompute-vs-submit row-lock semantics require PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_recompute_and_final_submission_use_one_consistent_locked_source(
    active_project, monkeypatch
):
    task = active_project.tasks.filter(relationship_type="manager").first()
    user = get_user_model().objects.create_user(
        username=f"recompute-race-{uuid4().hex}@example.test"
    )
    task.evaluator.user = user
    task.evaluator.save(update_fields=["user"])
    answers = {
        item["snapshot_item_id"]: 5
        for item in task.project_subject.template_snapshot["items"]
    }
    tasks_locked = Event()
    release_recompute = Event()
    submit_started = Event()
    submit_lock_attempted = Event()
    submit_done = Event()
    captured_sources = []
    original_lock = recompute_project._lock_project_tasks
    original_load = recompute_project.load_project_scoring_source
    original_submit_lock = submission_service._lock_task

    def pause_after_real_task_locks(project):
        tasks = original_lock(project)
        tasks_locked.set()
        assert release_recompute.wait(timeout=10)
        return tasks

    def capture_locked_source(project, *, tasks):
        source = original_load(project, tasks=tasks)
        captured_sources.append(source)
        return source

    def observe_submit_lock_attempt(submit_task_instance):
        submit_lock_attempted.set()
        return original_submit_lock(submit_task_instance)

    monkeypatch.setattr(
        recompute_project, "_lock_project_tasks", pause_after_real_task_locks
    )
    monkeypatch.setattr(
        recompute_project, "load_project_scoring_source", capture_locked_source
    )
    monkeypatch.setattr(
        submission_service, "_lock_task", observe_submit_lock_attempt
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recompute_future = pool.submit(
            _concurrent_recompute, str(active_project.public_id)
        )
        assert tasks_locked.wait(timeout=10)
        submit_future = pool.submit(
            _concurrent_submit,
            task.pk,
            user.pk,
            answers,
            submit_started,
            submit_done,
        )
        assert submit_started.wait(timeout=10)
        assert submit_lock_attempted.wait(timeout=10)
        assert not submit_done.wait(timeout=0.5)
        release_recompute.set()
        recompute_future.result(timeout=20)
        submit_future.result(timeout=20)

    persisted = AggregateResult.objects.get(
        project=active_project,
        project_subject=task.project_subject,
        algorithm_version=ALGORITHM_VERSION,
    )
    locked_outcome = calculate_project_results(captured_sources[0])[0][1]
    fresh_outcome = calculate_subject_result(active_project, task.subject)
    assert persisted.valid_submission_count == 0
    assert persisted.missing_groups == [
        "manager",
        "same_department",
        "cross_department",
    ]
    assert persisted.input_fingerprint == locked_outcome.input_fingerprint
    assert fresh_outcome.valid_submission_count == 1
    assert fresh_outcome.input_fingerprint != persisted.input_fingerprint
