from hashlib import sha256

import pytest
from django.db import transaction
from django.db.models.query import QuerySet

from apps.roster import imports as roster_imports
from apps.roster.imports import (
    ImportCommitError,
    commit_import_batch,
    preview_roster_upload,
)
from apps.roster.models import Employee, EmployeeCategory, ImportBatch
from tests.helpers import build_csv_bytes, ROSTER_HEADERS


@pytest.mark.django_db
def test_roster_preflight_uses_shared_employee_then_category_lock_order(
    roster_workbook_bytes, employee_set, hr_admin, monkeypatch
):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    lock_queries = []
    original_fetch_all = QuerySet._fetch_all

    def record_fetch_all(queryset):
        if (
            queryset._result_cache is None
            and queryset.query.select_for_update
            and queryset.model in (Employee, EmployeeCategory)
        ):
            lock_queries.append(
                (
                    queryset.model._meta.label_lower,
                    queryset.query.select_for_update_of,
                    queryset.query.order_by,
                )
            )
        return original_fetch_all(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", record_fetch_all)

    with transaction.atomic():
        roster_imports._preflight_roster(batch, "append")

    assert lock_queries == [
        ("roster.employee", ("self",), ("pk",)),
        ("roster.employeecategory", ("self",), ("pk",)),
    ]


@pytest.mark.django_db
def test_roster_preview_requires_explicit_duplicate_policy(
    roster_workbook_bytes, employee_set, hr_admin
):
    before = Employee.objects.count()
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    assert Employee.objects.count() == before
    assert batch.valid_count == 2
    assert batch.duplicate_count == 1
    assert batch.file_sha256 == sha256(roster_workbook_bytes).hexdigest()
    assert batch.created_by == hr_admin
    assert set(batch.rows[0]) == {
        "row_number",
        "employee_no",
        "name",
        "corporate_email",
        "department_level_1",
        "department_level_2",
        "category_id",
        "wecom_userid",
        "existing_employee_id",
        "existing_employee_fingerprint",
        "category_fingerprint",
    }

    with pytest.raises(ImportCommitError, match="重复处理策略"):
        commit_import_batch(
            batch.public_id, mode="append", duplicate_policy=None, actor=hr_admin
        )

    result = commit_import_batch(
        batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
    )
    assert result.skipped_duplicate_count == 1
    assert Employee.objects.count() == before + 1


@pytest.mark.django_db
def test_duplicate_policy_update_updates_existing_and_creates_new(
    roster_workbook_bytes, employee_set, hr_admin
):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    result = commit_import_batch(
        batch.public_id, mode="append", duplicate_policy="update", actor=hr_admin
    )

    employee_set[0].refresh_from_db()
    assert employee_set[0].name == "Updated Employee One"
    assert employee_set[0].wecom_userid == "wx_e001_updated"
    assert Employee.objects.filter(employee_no="E004", is_active=True).exists()
    assert result.updated_count == 1
    assert result.created_count == 1


@pytest.mark.django_db
def test_replace_preview_lists_only_missing_active_employees_for_deactivation(
    roster_workbook_bytes, employee_set, hr_admin
):
    inactive = employee_set[2]
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])

    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    assert batch.deactivate_count == 1
    assert batch.preview["deactivate_employee_nos"] == ["E002"]

    before_ids = set(Employee.objects.values_list("id", flat=True))
    commit_import_batch(
        batch.public_id, mode="replace", duplicate_policy="skip", actor=hr_admin
    )
    assert set(Employee.objects.values_list("id", flat=True)) >= before_ids
    assert Employee.objects.get(employee_no="E002").is_active is False
    assert Employee.objects.get(employee_no="E003").is_active is False


@pytest.mark.django_db
def test_import_batch_is_one_shot(roster_workbook_bytes, employee_set, hr_admin):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    commit_import_batch(
        batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
    )

    with pytest.raises(ImportCommitError, match="已提交"):
        commit_import_batch(
            batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
        )

    assert ImportBatch.objects.get(pk=batch.pk).status == ImportBatch.Status.COMMITTED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mode", "duplicate_policy"),
    [("merge", "skip"), ("append", "overwrite")],
)
def test_commit_rejects_unknown_mode_or_duplicate_policy(
    mode, duplicate_policy, roster_workbook_bytes, employee_set, hr_admin
):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    with pytest.raises(ImportCommitError):
        commit_import_batch(
            batch.public_id,
            mode=mode,
            duplicate_policy=duplicate_policy,
            actor=hr_admin,
        )


@pytest.mark.django_db
def test_roster_preview_records_controlled_issues_and_blocks_commit(employee_set, hr_admin):
    content = build_csv_bytes(
        ROSTER_HEADERS,
        [
            (
                "E900",
                "Invalid Category Employee",
                "e900@example.test",
                "Test Center",
                "Test Team",
                "MISSING",
                "wx_e900",
            )
        ],
    )

    batch = preview_roster_upload(content, "roster.csv", hr_admin)

    assert batch.valid_count == 0
    assert batch.issue_count == 1
    assert set(batch.issues.values_list("code", flat=True)) == {"CATEGORY_NOT_FOUND"}
    assert "e900@example.test" not in batch.issues.get().message
    with pytest.raises(ImportCommitError, match="异常"):
        commit_import_batch(
            batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin
        )


@pytest.mark.django_db
def test_roster_commit_rejects_same_pk_lost_update_before_any_write(
    roster_workbook_bytes, employee_set, hr_admin
):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    employee_set[0].name = "Concurrent HR Edit"
    employee_set[0].save(update_fields=["name"])

    with pytest.raises(ImportCommitError, match="业务数据已变化"):
        commit_import_batch(
            batch.public_id, mode="append", duplicate_policy="update", actor=hr_admin
        )

    employee_set[0].refresh_from_db()
    batch.refresh_from_db()
    assert employee_set[0].name == "Concurrent HR Edit"
    assert Employee.objects.filter(employee_no="E004").exists() is False
    assert batch.status == ImportBatch.Status.PREVIEWED


@pytest.mark.django_db
def test_roster_replace_rejects_changed_deactivation_target(
    roster_workbook_bytes, employee_set, hr_admin
):
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    target = employee_set[1]
    target.department_level_2 = "Concurrent Team"
    target.save(update_fields=["department_level_2"])

    with pytest.raises(ImportCommitError, match="业务数据已变化"):
        commit_import_batch(
            batch.public_id, mode="replace", duplicate_policy="skip", actor=hr_admin
        )

    target.refresh_from_db()
    assert target.department_level_2 == "Concurrent Team"
    assert target.is_active is True
    assert Employee.objects.filter(employee_no="E004").exists() is False
