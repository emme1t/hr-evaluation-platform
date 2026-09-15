import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import QuerySet
from django.urls import reverse

from apps.roster.models import Employee, EmployeeCategory, EvaluationRelationship, ImportBatch
from apps.roster.services import (
    RosterValidationError,
    create_category_record,
    create_employee_record,
    deactivate_employee,
    deactivate_relationship,
    update_category_record,
    update_employee_record,
    update_relationship_record,
    upsert_relationship,
)


@pytest.mark.django_db
def test_manual_employee_rejects_inactive_category(hr_admin):
    category = EmployeeCategory.objects.create(
        code="OLD", name="已停用类别", is_active=False
    )

    with pytest.raises(RosterValidationError, match="员工类别已停用"):
        create_employee_record(
            employee_no="E900",
            name="测试人员",
            corporate_email="e900@example.test",
            department_level_1="测试中心",
            department_level_2="测试组",
            category_id=category.id,
            actor=hr_admin,
        )


@pytest.mark.django_db
def test_manual_relationship_matches_by_employee_number(employee_set, hr_admin):
    relation = upsert_relationship(
        subject_no=employee_set[0].employee_no,
        evaluator_no=employee_set[1].employee_no,
        relationship_type="same_department",
        actor=hr_admin,
    )

    assert relation.subject_id == employee_set[0].id
    assert relation.evaluator_id == employee_set[1].id


@pytest.mark.django_db
def test_standalone_upsert_locks_employees_before_matching_relationships(
    employee_set, hr_admin, monkeypatch
):
    inactive = EvaluationRelationship.objects.create(
        subject=employee_set[0],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
        is_active=False,
    )
    lock_queries = []
    original_fetch_all = QuerySet._fetch_all

    def record_fetch_all(queryset):
        if (
            queryset._result_cache is None
            and queryset.query.select_for_update
            and queryset.model in (Employee, EvaluationRelationship)
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

    restored = upsert_relationship(
        subject_no=employee_set[0].employee_no,
        evaluator_no=employee_set[1].employee_no,
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
        actor=hr_admin,
    )

    inactive.refresh_from_db()
    assert restored.pk == inactive.pk
    assert inactive.is_active is True
    assert lock_queries == [
        ("roster.employee", ("self",), ("pk",)),
        ("roster.evaluationrelationship", ("self",), ("pk",)),
    ]


@pytest.mark.django_db
def test_relationship_fk_update_locks_employees_before_relationships(
    employee_set, hr_admin, monkeypatch
):
    relationship = EvaluationRelationship.objects.create(
        subject=employee_set[0],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
    )
    lock_queries = []
    original_fetch_all = QuerySet._fetch_all

    def record_fetch_all(queryset):
        if (
            queryset._result_cache is None
            and queryset.query.select_for_update
            and queryset.model in (Employee, EvaluationRelationship)
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

    updated = update_relationship_record(
        relationship.pk,
        subject_no=employee_set[0].employee_no,
        evaluator_no=employee_set[2].employee_no,
        relationship_type=EvaluationRelationship.Type.MANAGER,
        actor=hr_admin,
    )

    assert updated.evaluator_id == employee_set[2].pk
    assert updated.relationship_type == EvaluationRelationship.Type.MANAGER
    assert lock_queries == [
        ("roster.employee", ("self",), ("pk",)),
        ("roster.evaluationrelationship", ("self",), ("pk",)),
    ]


@pytest.mark.django_db
def test_manual_services_update_and_only_deactivate_records(employee_set, hr_admin):
    category = create_category_record(code="NEW", name="新类别", actor=hr_admin)
    update_category_record(category.id, code="NEW2", name="新类别二", actor=hr_admin)
    employee = create_employee_record(
        employee_no="E900",
        name="测试人员",
        corporate_email="e900@example.test",
        department_level_1="测试中心",
        department_level_2="测试组",
        category_id=category.id,
        wecom_userid="wx_e900",
        actor=hr_admin,
    )
    update_employee_record(
        employee.id,
        employee_no="E900",
        name="更新人员",
        corporate_email="e900@example.test",
        department_level_1="测试中心",
        department_level_2="更新组",
        category_id=category.id,
        wecom_userid="wx_e900",
        actor=hr_admin,
    )

    deactivate_employee(employee.id, actor=hr_admin)
    employee.refresh_from_db()
    category.refresh_from_db()
    assert employee.name == "更新人员"
    assert employee.is_active is False
    assert category.code == "NEW2"
    assert Employee.objects.filter(pk=employee.pk).exists()


@pytest.mark.django_db
def test_manual_employee_rejects_conflicting_unique_identity(employee_set, hr_admin):
    with pytest.raises(RosterValidationError, match="企业邮箱已存在"):
        create_employee_record(
            employee_no="E900",
            name="测试人员",
            corporate_email=employee_set[0].corporate_email,
            department_level_1="测试中心",
            department_level_2="测试组",
            category_id=employee_set[0].category_id,
            actor=hr_admin,
        )


@pytest.mark.django_db
def test_manual_relationship_rejects_active_duplicate(employee_set, hr_admin):
    upsert_relationship(
        subject_no="E001",
        evaluator_no="E002",
        relationship_type="same_department",
        actor=hr_admin,
    )

    with pytest.raises(RosterValidationError, match="协作关系已存在"):
        upsert_relationship(
            subject_no="E001",
            evaluator_no="E002",
            relationship_type="same_department",
            actor=hr_admin,
        )


@pytest.mark.django_db
def test_deactivate_relationship_preserves_record(employee_set, hr_admin):
    relation = upsert_relationship(
        subject_no="E001",
        evaluator_no="E002",
        relationship_type="same_department",
        actor=hr_admin,
    )

    deactivate_relationship(relation.id, actor=hr_admin)

    assert EvaluationRelationship.objects.get(pk=relation.pk).is_active is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "route_name",
    ["roster-list", "category-list", "relationship-list"],
)
def test_hr_list_gets_do_not_mutate_business_data(client, hr_admin, employee_set, route_name):
    client.force_login(hr_admin)
    before = (
        Employee.objects.count(),
        EmployeeCategory.objects.count(),
        EvaluationRelationship.objects.count(),
        ImportBatch.objects.count(),
    )

    response = client.get(reverse(f"roster:{route_name}"))

    assert response.status_code == 200
    assert before == (
        Employee.objects.count(),
        EmployeeCategory.objects.count(),
        EvaluationRelationship.objects.count(),
        ImportBatch.objects.count(),
    )


@pytest.mark.django_db
def test_non_hr_user_cannot_access_roster_pages(client, user_with_role):
    client.force_login(user_with_role)

    assert client.get(reverse("roster:roster-list")).status_code == 403
    assert client.post(reverse("roster:employee-create"), {}).status_code == 403


@pytest.mark.django_db
def test_anonymous_user_is_redirected_to_existing_wecom_login(client):
    response = client.get(reverse("roster:roster-list"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts:wecom_start"))


@pytest.mark.django_db
def test_deactivation_endpoint_requires_post(client, hr_admin, employee_set):
    client.force_login(hr_admin)
    url = reverse("roster:employee-deactivate", args=[employee_set[0].id])

    assert client.get(url).status_code == 405
    employee_set[0].refresh_from_db()
    assert employee_set[0].is_active is True

    assert client.post(url).status_code == 302
    employee_set[0].refresh_from_db()
    assert employee_set[0].is_active is False


@pytest.mark.django_db
def test_mutating_endpoint_enforces_csrf(client, hr_admin, employee_set):
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(hr_admin)

    response = csrf_client.post(
        reverse("roster:employee-deactivate", args=[employee_set[0].id])
    )

    assert response.status_code == 403
    employee_set[0].refresh_from_db()
    assert employee_set[0].is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("roster.txt", b"not-a-workbook"),
        ("roster.xlsx", b"x" * (10 * 1024 * 1024 + 1)),
    ],
    ids=("disallowed-extension", "over-10mb"),
)
def test_roster_upload_rejects_disallowed_type_or_oversize(
    client, hr_admin, filename, content
):
    client.force_login(hr_admin)

    response = client.post(
        reverse("roster:roster-upload"),
        {"file": SimpleUploadedFile(filename, content)},
    )

    assert response.status_code == 400
    assert ImportBatch.objects.count() == 0


@pytest.mark.django_db
def test_roster_upload_previews_without_writing_employees(
    client, hr_admin, employee_set, roster_workbook_bytes
):
    client.force_login(hr_admin)
    before = Employee.objects.count()

    response = client.post(
        reverse("roster:roster-upload"),
        {"file": SimpleUploadedFile("roster.xlsx", roster_workbook_bytes)},
    )

    assert response.status_code == 200
    assert Employee.objects.count() == before
    assert response.context["batch"].valid_count == 2


@pytest.mark.django_db
def test_replace_commit_page_requires_secondary_confirmation(
    client, hr_admin, employee_set, roster_workbook_bytes
):
    from apps.roster.imports import preview_roster_upload

    client.force_login(hr_admin)
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    response = client.post(
        reverse("roster:import-commit", args=[batch.public_id]),
        {"mode": "replace", "duplicate_policy": "skip"},
    )

    assert response.status_code == 400
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.PREVIEWED

    response = client.post(
        reverse("roster:import-commit", args=[batch.public_id]),
        {
            "mode": "replace",
            "duplicate_policy": "skip",
            "confirm_replace": "yes",
        },
    )
    assert response.status_code == 302


@pytest.mark.django_db
def test_replace_preview_renders_complete_employee_deactivation_set(
    client, hr_admin, employee_set, roster_workbook_bytes
):
    from apps.roster.imports import preview_roster_upload

    client.force_login(hr_admin)
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    response = client.get(reverse("roster:import-preview", args=[batch.public_id]))
    content = response.content.decode()

    assert response.status_code == 200
    assert "E002" in content and "Test Employee Two" in content
    assert "E003" in content and "Test Employee Three" in content
    assert content.count('data-deactivation-target="employee"') == 2


@pytest.mark.django_db
def test_replace_preview_renders_complete_relationship_deactivation_set(
    client, hr_admin, employee_set, relationship_workbook_bytes
):
    from apps.roster.imports import preview_relationship_upload

    relationship = EvaluationRelationship.objects.create(
        subject=employee_set[2],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.MANAGER,
    )
    client.force_login(hr_admin)
    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )

    response = client.get(reverse("roster:import-preview", args=[batch.public_id]))
    content = response.content.decode()

    assert response.status_code == 200
    assert relationship.subject.employee_no in content
    assert relationship.subject.name in content
    assert relationship.evaluator.employee_no in content
    assert relationship.evaluator.name in content
    assert "上级" in content
    assert content.count('data-deactivation-target="relationship"') == 1


@pytest.mark.django_db
def test_replace_confirmation_rejects_batch_without_complete_target_details(
    client, hr_admin, employee_set, roster_workbook_bytes
):
    from apps.roster.imports import preview_roster_upload

    client.force_login(hr_admin)
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    batch.preview["deactivate_employees"] = [
        {"id": employee_id}
        for employee_id in batch.preview["deactivate_employee_ids"]
    ]
    batch.save(update_fields=["preview"])

    response = client.post(
        reverse("roster:import-commit", args=[batch.public_id]),
        {
            "mode": "replace",
            "duplicate_policy": "skip",
            "confirm_replace": "yes",
        },
    )

    assert response.status_code == 400
    assert "请重新预检" in response.content.decode()
    assert Employee.objects.get(employee_no="E002").is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("name", ""),
        ("department_level_1", "Wrong Center"),
        ("department_level_2", "Wrong Team"),
    ],
)
def test_replace_preview_rejects_tampered_employee_display_payload(
    client,
    hr_admin,
    employee_set,
    roster_workbook_bytes,
    field,
    tampered_value,
):
    from apps.roster.imports import (
        ImportCommitError,
        commit_import_batch,
        preview_roster_upload,
    )

    client.force_login(hr_admin)
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)
    batch.preview["deactivate_employees"][0][field] = tampered_value
    batch.save(update_fields=["preview"])

    response = client.get(reverse("roster:import-preview", args=[batch.public_id]))

    assert response.status_code == 400
    assert "确认提交" not in response.content.decode()
    with pytest.raises(ImportCommitError, match="预览.*重新预检"):
        commit_import_batch(
            batch.public_id, mode="replace", duplicate_policy="skip", actor=hr_admin
        )
    assert Employee.objects.get(employee_no="E002").is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("subject_no", "E999"),
        ("subject_name", ""),
        ("evaluator_name", "Wrong Evaluator"),
        ("relationship_type_label", "错误类型"),
    ],
)
def test_replace_preview_rejects_tampered_relationship_display_payload(
    client,
    hr_admin,
    employee_set,
    relationship_workbook_bytes,
    field,
    tampered_value,
):
    from apps.roster.imports import (
        ImportCommitError,
        commit_import_batch,
        preview_relationship_upload,
    )

    relationship = EvaluationRelationship.objects.create(
        subject=employee_set[2],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.MANAGER,
    )
    client.force_login(hr_admin)
    batch = preview_relationship_upload(
        relationship_workbook_bytes, "relations.xlsx", hr_admin
    )
    batch.preview["deactivate_relationships"][0][field] = tampered_value
    batch.save(update_fields=["preview"])

    response = client.get(reverse("roster:import-preview", args=[batch.public_id]))

    assert response.status_code == 400
    assert "确认提交" not in response.content.decode()
    with pytest.raises(ImportCommitError, match="预览.*重新预检"):
        commit_import_batch(
            batch.public_id, mode="replace", duplicate_policy="update", actor=hr_admin
        )
    relationship.refresh_from_db()
    assert relationship.is_active is True


@pytest.mark.django_db
def test_employee_unique_constraint_race_becomes_stable_validation_error(
    monkeypatch, employee_set, hr_admin
):
    original_exists = QuerySet.exists

    def hide_employee_precheck(queryset):
        if queryset.model is Employee:
            return False
        return original_exists(queryset)

    monkeypatch.setattr(QuerySet, "exists", hide_employee_precheck)

    with pytest.raises(RosterValidationError, match="企业邮箱已存在"):
        create_employee_record(
            employee_no="E900",
            name="Race Employee",
            corporate_email=employee_set[0].corporate_email,
            department_level_1="Test Center",
            department_level_2="Test Team",
            category_id=employee_set[0].category_id,
            actor=hr_admin,
        )


@pytest.mark.django_db
def test_category_unique_constraint_race_becomes_stable_validation_error(
    monkeypatch, employee_set, hr_admin
):
    original_exists = QuerySet.exists

    def hide_category_precheck(queryset):
        if queryset.model is EmployeeCategory:
            return False
        return original_exists(queryset)

    monkeypatch.setattr(QuerySet, "exists", hide_category_precheck)

    with pytest.raises(RosterValidationError, match="类别编码已存在"):
        create_category_record(code="TEST", name="Race Category", actor=hr_admin)


@pytest.mark.django_db
def test_relationship_unique_constraint_race_becomes_stable_validation_error(
    monkeypatch, employee_set, hr_admin
):
    EvaluationRelationship.objects.create(
        subject=employee_set[0],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
    )
    original_fetch_all = QuerySet._fetch_all
    hidden = False

    def hide_locked_relationship_once(queryset):
        nonlocal hidden
        if (
            queryset._result_cache is None
            and queryset.model is EvaluationRelationship
            and queryset.query.select_for_update
            and not hidden
        ):
            hidden = True
            queryset._result_cache = []
            return
        return original_fetch_all(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", hide_locked_relationship_once)

    with pytest.raises(RosterValidationError, match="协作关系已存在"):
        upsert_relationship(
            subject_no="E001",
            evaluator_no="E002",
            relationship_type="same_department",
            actor=hr_admin,
        )
    assert EvaluationRelationship.objects.filter(
        subject=employee_set[0],
        evaluator=employee_set[1],
        relationship_type=EvaluationRelationship.Type.SAME_DEPARTMENT,
    ).count() == 1
