from copy import deepcopy
from uuid import UUID

import pytest
from django.db import models
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.evaluations.models import EvaluationTask, ProjectSubject
from apps.evaluations.services import projects as project_services
from apps.evaluations.services.projects import (
    ProjectValidationError,
    ProjectStateError,
    prepare_project,
    preview_project_tasks,
    validate_ready_project_integrity,
)
from apps.roster.models import Employee, EvaluationRelationship
from tests.factories import (
    create_category,
    create_employee,
    create_project,
    create_relationship,
    create_template,
)


def _corrupt_ready_project(project, component):
    project_subject = project.subjects.get()
    if component == "prepared_at":
        project.prepared_at = None
        project.save(update_fields=["prepared_at"])
    elif component == "rules":
        project.rule_snapshot["manager"] = "0.500"
        project.save(update_fields=["rule_snapshot"])
    elif component == "required_groups":
        project.rule_snapshot.pop("required_groups")
        project.save(update_fields=["rule_snapshot"])
    elif component == "rule_weight":
        project.rule_snapshot["cross_department"] = "0.10"
        project.save(update_fields=["rule_snapshot"])
    elif component == "subject_snapshot":
        project_subject.subject_snapshot = {}
        project_subject.save(update_fields=["subject_snapshot"])
    elif component == "template_snapshot":
        project_subject.template_snapshot = {"version": 1, "items": []}
        project_subject.save(update_fields=["template_snapshot"])
    elif component == "template_item_id":
        snapshot = deepcopy(project_subject.template_snapshot)
        snapshot["items"][0]["snapshot_item_id"] = "not-a-uuid"
        project_subject.template_snapshot = snapshot
        project_subject.save(update_fields=["template_snapshot"])
    elif component == "relationship_item_id":
        snapshot = deepcopy(project_subject.relationship_snapshot)
        snapshot[0]["snapshot_item_id"] = "not-a-uuid"
        project_subject.relationship_snapshot = snapshot
        project_subject.save(update_fields=["relationship_snapshot"])
    elif component == "duplicate_relationship_snapshot":
        snapshot = deepcopy(project_subject.relationship_snapshot)
        snapshot.append(deepcopy(snapshot[0]))
        project_subject.relationship_snapshot = snapshot
        project_subject.save(update_fields=["relationship_snapshot"])
    elif component == "missing_task":
        project.tasks.order_by("pk").first().delete()
    elif component == "extra_task":
        snapshot = deepcopy(project_subject.relationship_snapshot)
        snapshot.pop()
        project_subject.relationship_snapshot = snapshot
        project_subject.save(update_fields=["relationship_snapshot"])
    elif component == "task_relationship_type":
        task = project.tasks.get(relationship_type="manager")
        task.relationship_type = "same_department"
        models.Model.save(task, update_fields=["relationship_type"])
    elif component == "task_project_link":
        task = project.tasks.order_by("pk").first()
        task.project = create_project(name="Other frozen project")
        models.Model.save(task, update_fields=["project"])
    elif component == "task_project_subject_link":
        task = project.tasks.order_by("pk").first()
        other_project = create_project(
            name="Other project subject",
            subject_templates=((project_subject.subject, project_subject.template),),
        )
        task.project_subject = other_project.subjects.get()
        models.Model.save(task, update_fields=["project_subject"])
    elif component == "task_subject_link":
        task = project.tasks.order_by("pk").first()
        task.subject = project.tasks.exclude(subject=task.subject).first().subject if project.tasks.exclude(subject=task.subject).exists() else task.evaluator
        models.Model.save(task, update_fields=["subject"])
    else:
        raise AssertionError(f"unknown corruption component: {component}")


def _replace_frozen_json(project, case):
    project_subject = project.subjects.get()
    rule_snapshot = deepcopy(project.rule_snapshot)
    subject_snapshot = deepcopy(project_subject.subject_snapshot)
    template_snapshot = deepcopy(project_subject.template_snapshot)
    relationship_snapshot = deepcopy(project_subject.relationship_snapshot)

    if case == "rule-list":
        project.rule_snapshot = []
    elif case == "rule-extra-key":
        rule_snapshot["unexpected"] = "value"
        project.rule_snapshot = rule_snapshot
    elif case == "required-groups-object":
        rule_snapshot["required_groups"] = {"manager": True}
        project.rule_snapshot = rule_snapshot
    elif case == "rule-weight-exponent":
        rule_snapshot["manager"] = "5e-1"
        project.rule_snapshot = rule_snapshot
    elif case == "subject-extra-key":
        subject_snapshot["employee_no"] = "must-not-be-frozen"
        project_subject.subject_snapshot = subject_snapshot
    elif case == "subject-list":
        project_subject.subject_snapshot = []
    elif case == "subject-public-id-list":
        subject_snapshot["public_id"] = []
        project_subject.subject_snapshot = subject_snapshot
    elif case == "template-extra-key":
        template_snapshot["unexpected"] = None
        project_subject.template_snapshot = template_snapshot
    elif case == "template-list":
        project_subject.template_snapshot = []
    elif case == "template-version-bool":
        template_snapshot["version"] = True
        project_subject.template_snapshot = template_snapshot
    elif case == "category-extra-key":
        template_snapshot["category"]["code"] = "must-not-be-frozen"
        project_subject.template_snapshot = template_snapshot
    elif case == "category-list":
        template_snapshot["category"] = []
        project_subject.template_snapshot = template_snapshot
    elif case == "items-object":
        template_snapshot["items"] = {"0": template_snapshot["items"][0]}
        project_subject.template_snapshot = template_snapshot
    elif case == "item-extra-key":
        template_snapshot["items"][0]["unexpected"] = "value"
        project_subject.template_snapshot = template_snapshot
    elif case == "item-list":
        template_snapshot["items"][0] = []
        project_subject.template_snapshot = template_snapshot
    elif case == "item-id-list":
        template_snapshot["items"][0]["snapshot_item_id"] = []
        project_subject.template_snapshot = template_snapshot
    elif case == "duplicate-item-id":
        template_snapshot["items"][1]["snapshot_item_id"] = (
            template_snapshot["items"][0]["snapshot_item_id"]
        )
        project_subject.template_snapshot = template_snapshot
    elif case == "item-order-bool":
        template_snapshot["items"][0]["order"] = True
        project_subject.template_snapshot = template_snapshot
    elif case == "duplicate-item-order":
        template_snapshot["items"][1]["order"] = (
            template_snapshot["items"][0]["order"]
        )
        project_subject.template_snapshot = template_snapshot
    elif case == "item-weight-bool":
        template_snapshot["items"][0]["weight"] = True
        project_subject.template_snapshot = template_snapshot
    elif case == "item-weight-malformed":
        template_snapshot["items"][0]["weight"] = "1.2.3"
        project_subject.template_snapshot = template_snapshot
    elif case == "item-weight-exponent":
        template_snapshot["items"][0]["weight"] = "5e-1"
        project_subject.template_snapshot = template_snapshot
    elif case == "item-score-bool":
        template_snapshot["items"][0]["score_min"] = False
        project_subject.template_snapshot = template_snapshot
    elif case == "relationship-object":
        project_subject.relationship_snapshot = {
            "0": relationship_snapshot[0]
        }
    elif case == "relationship-extra-key":
        relationship_snapshot[0]["unexpected"] = "value"
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "relationship-entry-list":
        relationship_snapshot[0] = []
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "relationship-id-list":
        relationship_snapshot[0]["snapshot_item_id"] = []
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "relationship-type-list":
        relationship_snapshot[0]["relationship_type"] = []
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "evaluator-extra-key":
        relationship_snapshot[0]["evaluator"]["employee_no"] = "forbidden"
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "evaluator-list":
        relationship_snapshot[0]["evaluator"] = []
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "evaluator-public-id-list":
        relationship_snapshot[0]["evaluator"]["public_id"] = []
        project_subject.relationship_snapshot = relationship_snapshot
    elif case == "duplicate-evaluator-mapping":
        relationship_snapshot[1]["evaluator"] = deepcopy(
            relationship_snapshot[0]["evaluator"]
        )
        project_subject.relationship_snapshot = relationship_snapshot
    else:
        raise AssertionError(f"unknown malformed JSON case: {case}")

    if case.startswith("rule") or case == "required-groups-object":
        project.save(update_fields=["rule_snapshot"])
    else:
        project_subject.save(
            update_fields=[
                "subject_snapshot",
                "template_snapshot",
                "relationship_snapshot",
            ]
        )


@pytest.mark.django_db
def test_prepare_freezes_complete_template_relationship_and_rule_snapshots(
    draft_project, hr_admin
):
    prepare_project(draft_project, hr_admin)

    draft_project.refresh_from_db()
    project_subject = draft_project.subjects.get()
    template_snapshot = project_subject.template_snapshot
    relation_snapshot = project_subject.relationship_snapshot

    assert draft_project.status == "ready"
    assert draft_project.rule_snapshot == {
        "manager": "0.50",
        "same_department": "0.30",
        "cross_department": "0.20",
        "required_groups": ["manager", "same_department", "cross_department"],
    }
    assert template_snapshot["version"] == 1
    assert template_snapshot["name"] == "Test evaluation template v1"
    assert len(template_snapshot["items"]) == 2
    assert {
        "snapshot_item_id",
        "group",
        "title",
        "order",
        "weight",
        "score_min",
        "score_max",
        "excellent_description",
        "good_description",
        "qualified_description",
        "improvement_description",
    } <= template_snapshot["items"][0].keys()
    assert UUID(template_snapshot["items"][0]["snapshot_item_id"])
    assert len(relation_snapshot) == 3
    assert all(UUID(item["snapshot_item_id"]) for item in relation_snapshot)
    assert project_subject.subject_snapshot["name"] == "Project Subject"


@pytest.mark.django_db
def test_snapshot_json_excludes_database_keys_and_employee_contact_identifiers(
    draft_project, hr_admin
):
    prepare_project(draft_project, hr_admin)
    project_subject = draft_project.subjects.get()
    forbidden = {"id", "pk", "employee_no", "corporate_email", "wecom_userid"}

    assert forbidden.isdisjoint(project_subject.subject_snapshot)
    assert forbidden.isdisjoint(project_subject.template_snapshot)
    for relation in project_subject.relationship_snapshot:
        assert forbidden.isdisjoint(relation)
        assert forbidden.isdisjoint(relation["evaluator"])


@pytest.mark.django_db
def test_later_relationship_and_template_changes_do_not_change_frozen_snapshot(
    draft_project, hr_admin
):
    prepare_project(draft_project, hr_admin)
    project_subject = draft_project.subjects.get()
    frozen_template = project_subject.template_snapshot.copy()
    frozen_relations = list(project_subject.relationship_snapshot)

    EvaluationRelationship.objects.filter(subject=project_subject.subject).update(
        is_active=False
    )
    create_template(
        category=project_subject.subject.category,
        version=2,
        created_by=hr_admin,
        item_weights=("1.00",),
    )

    project_subject.refresh_from_db()
    assert project_subject.template_snapshot == frozen_template
    assert project_subject.relationship_snapshot == frozen_relations


@pytest.mark.django_db
def test_real_sealed_template_with_order_zero_remains_valid_after_prepare(
    draft_project, hr_admin
):
    project_subject = draft_project.subjects.select_related(
        "subject__category"
    ).get()
    zero_order_category = create_category(
        "ORDER-ZERO", "Order Zero Contract Category"
    )
    project_subject.subject.category = zero_order_category
    project_subject.subject.save(update_fields=["category"])
    zero_order_template = create_template(
        category=zero_order_category,
        created_by=hr_admin,
        item_orders=(0, 1),
    )
    project_subject.template = zero_order_template
    project_subject.save(update_fields=["template"])

    first = prepare_project(draft_project, hr_admin)
    preview = preview_project_tasks(draft_project)
    second = prepare_project(draft_project, hr_admin)

    project_subject.refresh_from_db()
    assert zero_order_template.is_sealed is True
    assert [
        item["order"] for item in project_subject.template_snapshot["items"]
    ] == [0, 1]
    assert preview.relation_anomalies == ()
    assert first.created_count == second.total_count == 3
    assert second.created_count == 0


@pytest.mark.django_db
def test_preview_is_read_only_and_reports_counts_unbound_and_relation_anomalies(
    draft_project,
):
    project_subject = draft_project.subjects.get()
    evaluator = EvaluationRelationship.objects.get(
        subject=project_subject.subject, relationship_type="same_department"
    ).evaluator
    evaluator.wecom_userid = None
    evaluator.save(update_fields=["wecom_userid"])
    evaluator.department_level_2 = "Mismatched Team"
    evaluator.save(update_fields=["department_level_2"])
    before = (
        EvaluationTask.objects.count(),
        ProjectSubject.objects.filter(template_snapshot={}).count(),
        draft_project.status,
    )

    preview = preview_project_tasks(draft_project)

    draft_project.refresh_from_db()
    assert preview.total_count == 3
    assert preview.counts_by_group == {
        "manager": 1,
        "same_department": 1,
        "cross_department": 1,
    }
    assert [item["name"] for item in preview.unbound_employees] == ["Project Peer"]
    assert any(
        anomaly["code"] == "SAME_DEPARTMENT_REQUIRED"
        for anomaly in preview.relation_anomalies
    )
    assert before == (
        EvaluationTask.objects.count(),
        ProjectSubject.objects.filter(template_snapshot={}).count(),
        draft_project.status,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid_case",
    ["inactive_subject", "inactive_evaluator", "invalid_template", "missing_group", "weight"],
)
def test_invalid_preflight_rolls_back_all_snapshots_tasks_and_status(
    draft_project, hr_admin, invalid_case
):
    project_subject = draft_project.subjects.get()
    if invalid_case == "inactive_subject":
        project_subject.subject.is_active = False
        project_subject.subject.save(update_fields=["is_active"])
    elif invalid_case == "inactive_evaluator":
        relation = EvaluationRelationship.objects.filter(subject=project_subject.subject).first()
        relation.evaluator.is_active = False
        relation.evaluator.save(update_fields=["is_active"])
    elif invalid_case == "invalid_template":
        project_subject.template.is_active = False
        models.Model.save(
            project_subject.template,
            update_fields=["is_active"],
            using=project_subject.template._state.db,
        )
    elif invalid_case == "missing_group":
        EvaluationRelationship.objects.filter(
            subject=project_subject.subject, relationship_type="cross_department"
        ).update(is_active=False)
    else:
        draft_project.rule_snapshot["cross_department"] = "0.10"
        draft_project.save(update_fields=["rule_snapshot"])

    with pytest.raises(ProjectValidationError):
        prepare_project(draft_project, hr_admin)

    draft_project.refresh_from_db()
    project_subject.refresh_from_db()
    assert draft_project.status == "draft"
    assert draft_project.prepared_at is None
    assert project_subject.subject_snapshot == {}
    assert project_subject.template_snapshot == {}
    assert project_subject.relationship_snapshot == []
    assert draft_project.tasks.count() == 0


@pytest.mark.django_db
def test_prepare_requires_at_least_one_subject(hr_admin):
    from tests.factories import create_project

    project = create_project()

    with pytest.raises(ProjectValidationError, match="至少需要一名被评价人"):
        prepare_project(project, hr_admin)


@pytest.mark.django_db
def test_prepare_rejects_non_hr_actor_without_writes(draft_project, user_with_role):
    from apps.core.permissions import HRPermissionError

    with pytest.raises(HRPermissionError):
        prepare_project(draft_project, user_with_role)

    assert draft_project.tasks.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "component",
    [
        "prepared_at",
        "rules",
        "required_groups",
        "rule_weight",
        "subject_snapshot",
        "template_snapshot",
        "template_item_id",
        "relationship_item_id",
        "duplicate_relationship_snapshot",
        "missing_task",
        "extra_task",
        "task_relationship_type",
        "task_project_link",
        "task_project_subject_link",
        "task_subject_link",
    ],
)
def test_ready_prepare_rejects_corrupt_frozen_data_and_preview_surfaces_anomaly(
    draft_project, hr_admin, component
):
    prepare_project(draft_project, hr_admin)
    _corrupt_ready_project(draft_project, component)

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)

    preview = preview_project_tasks(draft_project)
    assert raised.value.code == "PROJECT_FROZEN_DATA_CORRUPT"
    assert any(
        anomaly["code"] == "FROZEN_DATA_CORRUPT"
        for anomaly in preview.relation_anomalies
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    [
        "rule-list",
        "rule-extra-key",
        "required-groups-object",
        "rule-weight-exponent",
        "subject-extra-key",
        "subject-list",
        "subject-public-id-list",
        "template-extra-key",
        "template-list",
        "template-version-bool",
        "category-extra-key",
        "category-list",
        "items-object",
        "item-extra-key",
        "item-list",
        "item-id-list",
        "duplicate-item-id",
        "item-order-bool",
        "duplicate-item-order",
        "item-weight-bool",
        "item-weight-malformed",
        "item-weight-exponent",
        "item-score-bool",
        "relationship-object",
        "relationship-extra-key",
        "relationship-entry-list",
        "relationship-id-list",
        "relationship-type-list",
        "evaluator-extra-key",
        "evaluator-list",
        "evaluator-public-id-list",
        "duplicate-evaluator-mapping",
    ],
)
def test_ready_validator_is_total_and_rejects_malformed_closed_schema_json(
    draft_project, hr_admin, case
):
    prepare_project(draft_project, hr_admin)
    _replace_frozen_json(draft_project, case)

    with pytest.raises(ProjectStateError) as raised:
        prepare_project(draft_project, hr_admin)
    preview = preview_project_tasks(draft_project)

    assert raised.value.code == "PROJECT_FROZEN_DATA_CORRUPT"
    assert preview.relation_anomalies
    assert {
        anomaly["code"] for anomaly in preview.relation_anomalies
    } == {"FROZEN_DATA_CORRUPT"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field",
    [
        "rule_snapshot",
        "subject_snapshot",
        "template_snapshot",
        "relationship_snapshot",
    ],
)
@pytest.mark.parametrize(
    "value",
    [None, False, 0, 1.5, "text", [], [None], {"unexpected": None}],
    ids=[
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "empty-list",
        "nested-list",
        "object",
    ],
)
def test_ready_validator_is_total_for_top_level_json_types(
    draft_project, hr_admin, field, value
):
    prepare_project(draft_project, hr_admin)
    project_subject = draft_project.subjects.get()
    tasks = list(draft_project.tasks.order_by("pk"))
    target = draft_project if field == "rule_snapshot" else project_subject
    setattr(target, field, deepcopy(value))

    with pytest.raises(ProjectStateError) as raised:
        validate_ready_project_integrity(
            draft_project, [project_subject], tasks
        )

    assert raised.value.code == "PROJECT_FROZEN_DATA_CORRUPT"


@pytest.mark.django_db
def test_ready_validator_does_not_convert_unexpected_runtime_error(
    draft_project, hr_admin, monkeypatch
):
    prepare_project(draft_project, hr_admin)

    def raise_programmer_error(*args, **kwargs):
        raise RuntimeError("injected frozen validator programmer error")

    monkeypatch.setattr(
        project_services,
        "_collect_frozen_data_integrity_errors",
        raise_programmer_error,
    )

    with pytest.raises(RuntimeError, match="programmer error"):
        prepare_project(draft_project, hr_admin)
    with pytest.raises(RuntimeError, match="programmer error"):
        preview_project_tasks(draft_project)


@pytest.mark.django_db
def test_preview_500_subjects_bulk_loads_template_items_with_bounded_queries(hr_admin):
    categories = [
        create_category("PERF-A", "Performance Category A"),
        create_category("PERF-B", "Performance Category B"),
    ]
    templates = {
        category.pk: create_template(category=category, created_by=hr_admin)
        for category in categories
    }
    manager = Employee(
        employee_no="PERF-M",
        name="Performance Manager",
        corporate_email="perf-manager@example.test",
        department_level_1="Performance Center",
        department_level_2="Performance Team",
        category=categories[0],
        wecom_userid="wx_perf_manager",
    )
    peer = Employee(
        employee_no="PERF-P",
        name="Performance Peer",
        corporate_email="perf-peer@example.test",
        department_level_1="Performance Center",
        department_level_2="Performance Team",
        category=categories[0],
        wecom_userid="wx_perf_peer",
    )
    cross = Employee(
        employee_no="PERF-C",
        name="Performance Cross Team",
        corporate_email="perf-cross@example.test",
        department_level_1="Other Center",
        department_level_2="Other Team",
        category=categories[1],
        wecom_userid="wx_perf_cross",
    )
    subjects = [
        Employee(
            employee_no=f"PERF-{index:03d}",
            name=f"Performance Subject {index:03d}",
            corporate_email=f"perf-{index:03d}@example.test",
            department_level_1="Performance Center",
            department_level_2="Performance Team",
            category=categories[index % 2],
            wecom_userid=f"wx_perf_{index:03d}",
        )
        for index in range(500)
    ]
    Employee.objects.bulk_create([manager, peer, cross, *subjects])
    project = create_project(name="500 subject query budget")
    ProjectSubject.objects.bulk_create(
        [
            ProjectSubject(
                project=project,
                subject=subject,
                template=templates[subject.category_id],
            )
            for subject in subjects
        ]
    )
    EvaluationRelationship.objects.bulk_create(
        [
            EvaluationRelationship(
                subject=subject,
                evaluator=evaluator,
                relationship_type=relationship_type,
            )
            for subject in subjects
            for evaluator, relationship_type in (
                (manager, "manager"),
                (peer, "same_department"),
                (cross, "cross_department"),
            )
        ]
    )

    with CaptureQueriesContext(connection) as queries:
        preview = preview_project_tasks(project)

    assert preview.total_count == 1500
    assert preview.relation_anomalies == ()
    assert len(queries) <= 8


@pytest.mark.django_db
def test_prepare_supports_multiple_subjects_categories_and_templates(
    draft_project, hr_admin
):
    category = create_category("SECOND", "Second Project Category")
    subject = create_employee(
        "SECOND-S",
        "Second Subject",
        category=category,
        wecom_userid="wx_second_subject",
    )
    manager = create_employee(
        "SECOND-M",
        "Second Manager",
        category=category,
        wecom_userid="wx_second_manager",
    )
    peer = create_employee(
        "SECOND-P",
        "Second Peer",
        category=category,
        wecom_userid="wx_second_peer",
    )
    cross = create_employee(
        "SECOND-C",
        "Second Cross",
        category=category,
        department_level_1="Second Other Center",
        department_level_2="Second Other Team",
        wecom_userid="wx_second_cross",
    )
    template = create_template(
        category=category, created_by=hr_admin, item_weights=("1.00",)
    )
    for evaluator, relationship_type in (
        (manager, "manager"),
        (peer, "same_department"),
        (cross, "cross_department"),
    ):
        create_relationship(
            subject=subject,
            evaluator=evaluator,
            relationship_type=relationship_type,
        )
    ProjectSubject.objects.create(
        project=draft_project, subject=subject, template=template
    )

    result = prepare_project(draft_project, hr_admin)

    assert result.created_count == 6
    assert draft_project.subjects.count() == 2
    assert {
        item.template_snapshot["category"]["name"]
        for item in draft_project.subjects.all()
    } == {"Project Test Category", "Second Project Category"}


@pytest.mark.django_db
def test_prepare_rejects_self_relationship_and_rolls_back(draft_project, hr_admin):
    project_subject = draft_project.subjects.get()
    create_relationship(
        subject=project_subject.subject,
        evaluator=project_subject.subject,
        relationship_type="manager",
    )

    with pytest.raises(ProjectValidationError, match="不能建立本人评价关系"):
        prepare_project(draft_project, hr_admin)

    assert draft_project.tasks.count() == 0
    assert project_subject.template_snapshot == {}


@pytest.mark.django_db
def test_prepare_rejects_same_evaluator_repeated_across_relationship_groups(
    draft_project, hr_admin
):
    project_subject = draft_project.subjects.get()
    manager = EvaluationRelationship.objects.get(
        subject=project_subject.subject, relationship_type="manager"
    ).evaluator
    create_relationship(
        subject=project_subject.subject,
        evaluator=manager,
        relationship_type="same_department",
    )

    preview = preview_project_tasks(draft_project)
    with pytest.raises(ProjectValidationError, match="同一评价人不能重复分配"):
        prepare_project(draft_project, hr_admin)

    assert any(
        anomaly["code"] == "DUPLICATE_EVALUATOR"
        for anomaly in preview.relation_anomalies
    )
    assert draft_project.tasks.count() == 0


@pytest.mark.django_db
def test_prepare_rejects_inactive_subject_category(draft_project, hr_admin):
    project_subject = draft_project.subjects.get()
    project_subject.subject.category.is_active = False
    project_subject.subject.category.save(update_fields=["is_active"])

    with pytest.raises(ProjectValidationError, match="员工类别已停用"):
        prepare_project(draft_project, hr_admin)

    assert draft_project.tasks.count() == 0


@pytest.mark.django_db
def test_prepare_rejects_template_category_mismatch(draft_project, hr_admin):
    other_category = create_category("OTHER-TPL", "Other Template Category")
    other_template = create_template(category=other_category, created_by=hr_admin)
    project_subject = draft_project.subjects.get()
    project_subject.template = other_template
    project_subject.save(update_fields=["template"])

    with pytest.raises(ProjectValidationError, match="模板与员工类别不匹配"):
        prepare_project(draft_project, hr_admin)

    assert draft_project.tasks.count() == 0


@pytest.mark.django_db
def test_snapshot_privacy_check_recurses_through_every_nested_value(
    draft_project, hr_admin
):
    project_subject = draft_project.subjects.get()
    employees = {
        project_subject.subject,
        *(
            relation.evaluator
            for relation in EvaluationRelationship.objects.filter(
                subject=project_subject.subject
            )
        ),
    }
    forbidden_keys = {"id", "pk", "employee_no", "corporate_email", "wecom_userid"}
    forbidden_values = {
        value
        for employee in employees
        for value in (
            employee.employee_no,
            employee.corporate_email,
            employee.wecom_userid,
        )
        if value
    }
    prepare_project(draft_project, hr_admin)
    project_subject.refresh_from_db()

    def assert_private(value):
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value)
            for nested in value.values():
                assert_private(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_private(nested)
        elif isinstance(value, str):
            assert value not in forbidden_values

    assert_private(
        {
            "subject": project_subject.subject_snapshot,
            "template": project_subject.template_snapshot,
            "relationships": project_subject.relationship_snapshot,
        }
    )
