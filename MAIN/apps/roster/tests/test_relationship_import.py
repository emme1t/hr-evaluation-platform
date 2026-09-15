import pytest
from django.db import IntegrityError, transaction

from apps.roster.imports import (
    ImportCommitError,
    commit_import_batch,
    preview_relationship_upload,
)
from apps.roster.models import EvaluationRelationship, ImportBatch
from apps.roster.services import deactivate_relationship


@pytest.mark.django_db
def test_relationship_preview_does_not_write_relationships(
    employee_set, relationship_workbook_bytes, hr_admin
):
    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )

    assert batch.valid_count == 3
    assert EvaluationRelationship.objects.count() == 0

    commit_import_batch(
        batch.public_id, mode="replace", duplicate_policy="update", actor=hr_admin
    )
    assert EvaluationRelationship.objects.filter(is_active=True).count() == 3


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case,expected_code",
    [
        ("self", "SELF_RELATION"),
        ("missing", "EMPLOYEE_NOT_FOUND"),
        ("same_department_mismatch", "SAME_DEPARTMENT_REQUIRED"),
        ("cross_department_mismatch", "CROSS_DEPARTMENT_REQUIRED"),
        ("duplicate", "DUPLICATE_RELATION"),
    ],
)
def test_relationship_preview_reports_invalid_rows(
    case, expected_code, relationship_case_bytes, hr_admin
):
    batch = preview_relationship_upload(
        relationship_case_bytes(case), "relations.xlsx", hr_admin
    )

    assert expected_code in set(batch.issues.values_list("code", flat=True))


@pytest.mark.django_db
def test_relationship_replace_only_deactivates_previewed_missing_records(
    employee_set, relationship_workbook_bytes, hr_admin
):
    missing = EvaluationRelationship.objects.create(
        subject=employee_set[2],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.MANAGER,
    )
    already_inactive = EvaluationRelationship.objects.create(
        subject=employee_set[2],
        evaluator=employee_set[0],
        relationship_type=EvaluationRelationship.Type.MANAGER,
        is_active=False,
    )

    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )

    assert batch.deactivate_count == 1
    assert batch.preview["deactivate_relationship_ids"] == [missing.id]
    commit_import_batch(
        batch.public_id, mode="replace", duplicate_policy="update", actor=hr_admin
    )
    missing.refresh_from_db()
    already_inactive.refresh_from_db()
    assert missing.is_active is False
    assert already_inactive.is_active is False
    assert EvaluationRelationship.objects.filter(pk=missing.pk).exists()


@pytest.mark.django_db
def test_inactive_relationship_can_be_reactivated_by_manual_upsert(employee_set, hr_admin):
    relation = EvaluationRelationship.objects.create(
        subject=employee_set[0],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
    )
    deactivate_relationship(relation.id, actor=hr_admin)

    from apps.roster.services import upsert_relationship

    restored = upsert_relationship(
        subject_no="E001",
        evaluator_no="E002",
        relationship_type="same_department",
        actor=hr_admin,
    )

    assert restored.pk == relation.pk
    assert restored.is_active is True


@pytest.mark.django_db
def test_relationship_commit_rejects_snapshot_drift_and_rolls_back_prior_rows(
    employee_set, relationship_workbook_bytes, hr_admin
):
    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )
    drifted = EvaluationRelationship.objects.create(
        subject=employee_set[0],
        evaluator=employee_set[2],
        relationship_type=EvaluationRelationship.Type.CROSS_DEPARTMENT,
        is_active=False,
    )

    with pytest.raises(ImportCommitError, match="业务数据已变化"):
        commit_import_batch(
            batch.public_id,
            mode="append",
            duplicate_policy="update",
            actor=hr_admin,
        )

    drifted.refresh_from_db()
    batch.refresh_from_db()
    assert drifted.is_active is False
    assert EvaluationRelationship.objects.filter(is_active=True).count() == 0
    assert batch.status == ImportBatch.Status.PREVIEWED


@pytest.mark.django_db
def test_relationship_model_allows_history_but_only_one_active_duplicate(employee_set):
    fields = {
        "subject": employee_set[0],
        "evaluator": employee_set[1],
        "relationship_type": EvaluationRelationship.Type.SAME_DEPARTMENT,
    }
    EvaluationRelationship.objects.create(**fields)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EvaluationRelationship.objects.create(**fields)

    EvaluationRelationship.objects.create(**fields, is_active=False)
    assert EvaluationRelationship.objects.count() == 2


@pytest.mark.django_db
def test_relationship_replace_rejects_repointed_deactivation_target(
    employee_set, relationship_workbook_bytes, hr_admin
):
    target = EvaluationRelationship.objects.create(
        subject=employee_set[2],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.MANAGER,
    )
    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )
    target.subject = employee_set[0]
    target.relationship_type = EvaluationRelationship.Type.CROSS_DEPARTMENT
    target.save(update_fields=["subject", "relationship_type"])

    with pytest.raises(ImportCommitError, match="业务数据已变化"):
        commit_import_batch(
            batch.public_id, mode="replace", duplicate_policy="update", actor=hr_admin
        )

    target.refresh_from_db()
    assert target.subject_id == employee_set[0].id
    assert target.relationship_type == EvaluationRelationship.Type.CROSS_DEPARTMENT
    assert target.is_active is True
    assert EvaluationRelationship.objects.filter(is_active=True).count() == 1
