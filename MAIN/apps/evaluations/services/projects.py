import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from apps.audit.services import record_audit
from apps.core.permissions import require_hr_actor
from apps.roster import locking as roster_locking
from apps.roster.models import Employee, EmployeeCategory, EvaluationRelationship
from apps.roster.services import RosterValidationError, validate_relationship_entities

from ..models.projects import (
    EvaluationProject,
    EvaluationTask,
    ProjectSubject,
    validate_task_associations,
)
from ..models.templates import TemplateItem
from .frozen_snapshots import (
    FrozenTemplateSnapshotValidationError,
    normalize_frozen_template_snapshot,
)
from .templates import validate_template_items


RULE_GROUPS = (
    EvaluationRelationship.Type.MANAGER,
    EvaluationRelationship.Type.SAME_DEPARTMENT,
    EvaluationRelationship.Type.CROSS_DEPARTMENT,
)
RETRYABLE_POSTGRESQL_SQLSTATES = frozenset({"40P01", "55P03", "40001"})
RULE_WEIGHT_PATTERN = re.compile(r"(?:0\.\d{2,5}|1\.00)\Z")
DEFAULT_RULES = {
    EvaluationRelationship.Type.MANAGER: "0.50",
    EvaluationRelationship.Type.SAME_DEPARTMENT: "0.30",
    EvaluationRelationship.Type.CROSS_DEPARTMENT: "0.20",
}

RULE_SNAPSHOT_KEYS = {*RULE_GROUPS, "required_groups"}
EMPLOYEE_SNAPSHOT_KEYS = {
    "public_id",
    "name",
    "department_level_1",
    "department_level_2",
}
RELATIONSHIP_SNAPSHOT_KEYS = {
    "snapshot_item_id",
    "relationship_type",
    "evaluator",
}
REPORTING_SNAPSHOT_KEYS = {
    "employee_no",
    "corporate_email",
    "department",
    "manager_name",
}


class ProjectError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class ProjectValidationError(ProjectError):
    def __init__(self, errors, code="PROJECT_INVALID"):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = tuple(errors)
        super().__init__("；".join(self.errors), code)


class ProjectStateError(ProjectError):
    def __init__(self, message, code="PROJECT_STATE_INVALID"):
        super().__init__(message, code)


class FrozenDataValidationError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.errors = (message,)


@dataclass(frozen=True)
class ProjectPreview:
    total_count: int
    counts_by_group: dict[str, int]
    unbound_employees: tuple[dict, ...]
    relation_anomalies: tuple[dict, ...]

    @property
    def counts(self):
        return self.counts_by_group

    @property
    def relationship_anomalies(self):
        return self.relation_anomalies


@dataclass(frozen=True)
class PrepareResult:
    created_count: int
    total_count: int


@dataclass(frozen=True)
class _SubjectPlan:
    project_subject: ProjectSubject
    subject_snapshot: dict
    template_snapshot: dict
    relationship_snapshot: list[dict]
    relations: tuple[EvaluationRelationship, ...]


@dataclass(frozen=True)
class _TemplateCacheEntry:
    validation_errors: tuple[str, ...]
    snapshot_payload: dict


def normalize_project_rules(raw_rules):
    errors = []
    values = {}
    for group in RULE_GROUPS:
        raw_value = (raw_rules or {}).get(group)
        if raw_value in (None, ""):
            errors.append(f"缺少{EvaluationRelationship.Type(group).label}权重")
            continue
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError):
            errors.append(f"{EvaluationRelationship.Type(group).label}权重无效")
            continue
        if not value.is_finite():
            errors.append(f"{EvaluationRelationship.Type(group).label}权重无效")
        elif value < 0:
            errors.append("关系权重不能为负数")
        values[group] = value
    if not errors and sum(values.values(), Decimal("0")) != Decimal("1.00"):
        errors.append("关系权重合计必须精确等于 1.00")
    if errors:
        raise ProjectValidationError(errors, "INVALID_RULES")
    return {
        **{group: _decimal_string(values[group]) for group in RULE_GROUPS},
        "required_groups": list(RULE_GROUPS),
    }


def _decimal_string(value):
    normalized = value.normalize()
    decimal_places = max(2, -normalized.as_tuple().exponent)
    return f"{normalized:.{decimal_places}f}"


def preview_project_tasks(project):
    project = EvaluationProject.objects.get(pk=project.pk)
    if project.status != EvaluationProject.Status.DRAFT:
        return _preview_frozen_project(project)
    project_subjects = list(
        ProjectSubject.objects.filter(project=project)
        .select_related("subject__category", "template")
        .order_by("pk")
    )
    return _analyze_project(project, project_subjects).preview


def _preview_frozen_project(project):
    project_subjects = list(project.subjects.order_by("pk"))
    tasks = list(project.tasks.order_by("pk"))
    integrity_errors = _frozen_data_integrity_errors(
        project, project_subjects, tasks
    )
    counts = {group: 0 for group in RULE_GROUPS}
    for task in tasks:
        if task.relationship_type in counts:
            counts[task.relationship_type] += 1
    unbound = _unbound_employee_payloads(
        Employee.objects.filter(tasks_to_complete__project=project).distinct()
    )
    return ProjectPreview(
        total_count=sum(counts.values()),
        counts_by_group=counts,
        unbound_employees=tuple(unbound),
        relation_anomalies=tuple(
            {
                "code": "FROZEN_DATA_CORRUPT",
                "message": message,
            }
            for message in integrity_errors
        ),
    )


def validate_ready_project_integrity(project, project_subjects=None, tasks=None):
    project_subjects = (
        list(project.subjects.order_by("pk"))
        if project_subjects is None
        else list(project_subjects)
    )
    tasks = list(project.tasks.order_by("pk")) if tasks is None else list(tasks)
    errors = _frozen_data_integrity_errors(project, project_subjects, tasks)
    if errors:
        raise ProjectStateError(
            "项目冻结数据不完整或不一致，请联系管理员",
            "PROJECT_FROZEN_DATA_CORRUPT",
        )
    return len(tasks)


def _collect_frozen_data_integrity_errors(project, project_subjects, tasks):
    errors = []
    if project.prepared_at is None:
        errors.append("项目缺少准备时间")
    errors.extend(_rule_snapshot_errors(project.rule_snapshot))
    if not project_subjects:
        errors.append("项目缺少被评价人快照")

    relation_items = {}
    project_subjects_by_id = {}
    for project_subject in project_subjects:
        project_subjects_by_id[project_subject.pk] = project_subject
        if project_subject.project_id != project.pk:
            errors.append("被评价人快照不属于当前项目")
        errors.extend(_subject_snapshot_errors(project_subject.subject_snapshot))
        errors.extend(_template_snapshot_errors(project_subject.template_snapshot))
        relationship_errors, items = _relationship_snapshot_errors(
            project_subject.relationship_snapshot
        )
        errors.extend(relationship_errors)
        for snapshot_id, relationship_type in items.items():
            key = (project_subject.pk, snapshot_id)
            if key in relation_items:
                errors.append("关系快照项重复")
            relation_items[key] = relationship_type

    task_items = {}
    for task in tasks:
        project_subject = project_subjects_by_id.get(task.project_subject_id)
        if task.project_id != project.pk or project_subject is None:
            errors.append("任务项目关联不一致")
            continue
        if task.subject_id != project_subject.subject_id:
            errors.append("任务被评价人关联不一致")
        if task.relationship_type not in EvaluationRelationship.Type.values:
            errors.append("任务关系类型无效")
        if task.status not in EvaluationTask.Status.values:
            errors.append("任务状态无效")
        key = (task.project_subject_id, str(task.relationship_snapshot_item_id))
        if key in task_items:
            errors.append("任务关系快照关联重复")
        task_items[key] = task.relationship_type
        if relation_items.get(key) != task.relationship_type:
            errors.append("任务与关系快照不一致")

    if set(task_items) != set(relation_items):
        errors.append("任务与关系快照数量或范围不一致")
    return errors


def _rule_snapshot_errors(snapshot):
    if not isinstance(snapshot, dict):
        raise FrozenDataValidationError("项目规则快照结构无效")
    if set(snapshot) != RULE_SNAPSHOT_KEYS:
        return ["项目规则快照结构无效"]
    if snapshot.get("required_groups") != list(RULE_GROUPS):
        return ["项目规则必需关系组无效"]
    if any(
        not isinstance(snapshot.get(group), str)
        or RULE_WEIGHT_PATTERN.fullmatch(snapshot[group]) is None
        for group in RULE_GROUPS
    ):
        return ["项目规则权重类型无效"]
    try:
        normalized = normalize_project_rules(snapshot)
    except ProjectValidationError:
        return ["项目规则快照无效"]
    if snapshot != normalized:
        return ["项目规则快照不规范"]
    return []


def _display_snapshot_errors(snapshot, label):
    if not isinstance(snapshot, dict):
        raise FrozenDataValidationError(f"{label}结构无效")
    if set(snapshot) != EMPLOYEE_SNAPSHOT_KEYS:
        return [f"{label}结构无效"]
    errors = []
    if not _is_canonical_uuid(snapshot.get("public_id")):
        errors.append(f"{label}标识无效")
    for field in EMPLOYEE_SNAPSHOT_KEYS - {"public_id"}:
        value = snapshot.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}显示字段无效")
            break
    return errors


def _subject_snapshot_errors(snapshot):
    return _display_snapshot_errors(snapshot, "被评价人快照")


def _template_snapshot_errors(snapshot):
    try:
        normalize_frozen_template_snapshot(snapshot)
    except FrozenTemplateSnapshotValidationError:
        return ["模板快照无效"]
    return []


def _relationship_snapshot_errors(snapshot):
    if not isinstance(snapshot, list):
        raise FrozenDataValidationError("关系快照结构无效")
    if not snapshot:
        return ["关系快照为空"], {}
    errors = []
    items = {}
    seen_relations = set()
    seen_evaluators = set()
    groups = set()
    for item in snapshot:
        if (
            not isinstance(item, dict)
            or set(item) != RELATIONSHIP_SNAPSHOT_KEYS
        ):
            errors.append("关系快照项结构无效")
            continue
        snapshot_id = item.get("snapshot_item_id")
        relationship_type = item.get("relationship_type")
        evaluator = item.get("evaluator")
        snapshot_id_valid = _is_canonical_uuid(snapshot_id)
        relationship_type_valid = (
            isinstance(relationship_type, str)
            and relationship_type in EvaluationRelationship.Type.values
        )
        if not snapshot_id_valid:
            errors.append("关系快照标识无效")
        elif snapshot_id in items:
            errors.append("关系快照标识重复")
        if not relationship_type_valid:
            errors.append("关系快照类型无效")
        evaluator_errors = _display_snapshot_errors(evaluator, "评价人快照")
        if evaluator_errors:
            errors.extend(evaluator_errors)
            evaluator_public_id = None
        else:
            evaluator_public_id = evaluator.get("public_id")
        if evaluator_public_id is not None:
            if evaluator_public_id in seen_evaluators:
                errors.append("关系快照评价人映射重复")
            else:
                seen_evaluators.add(evaluator_public_id)
        if evaluator_public_id is not None and relationship_type_valid:
            relation_key = (evaluator_public_id, relationship_type)
            if relation_key in seen_relations:
                errors.append("关系快照评价关系重复")
            else:
                seen_relations.add(relation_key)
        if snapshot_id_valid and relationship_type_valid:
            if snapshot_id not in items:
                items[snapshot_id] = relationship_type
            groups.add(relationship_type)
    if not set(RULE_GROUPS).issubset(groups):
        errors.append("关系快照缺少必需关系组")
    return errors, items


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _frozen_data_integrity_errors(project, project_subjects, tasks):
    """Return corruption details for any JSON-serializable frozen payload.

    Database reads happen before this boundary. Only intentionally raised
    frozen-data validation errors are converted to corruption details.
    """
    try:
        return _collect_frozen_data_integrity_errors(
            project, project_subjects, tasks
        )
    except FrozenDataValidationError as exc:
        return list(exc.errors)


@dataclass(frozen=True)
class _Analysis:
    preview: ProjectPreview
    subject_plans: tuple[_SubjectPlan, ...]


def _analyze_project(project, project_subjects):
    anomalies = []
    plans = []
    counts = {group: 0 for group in RULE_GROUPS}
    unbound_employees = {}
    if not project_subjects:
        anomalies.append(
            {"code": "NO_SUBJECTS", "message": "项目至少需要一名被评价人"}
        )

    try:
        normalize_project_rules(project.rule_snapshot)
    except ProjectValidationError as exc:
        anomalies.extend(
            {"code": exc.code, "message": message} for message in exc.errors
        )

    relations_by_subject = _relations_by_subject(project_subjects)
    template_cache = _build_template_cache(project_subjects)
    for project_subject in project_subjects:
        subject = project_subject.subject
        template = project_subject.template
        cached_template = template_cache[template.pk]
        subject_errors = _validate_subject_and_template(
            subject, template, cached_template.validation_errors
        )
        anomalies.extend(
            {
                "code": code,
                "message": message,
                "subject_public_id": str(subject.public_id),
            }
            for code, message in subject_errors
        )

        valid_groups = set()
        relation_snapshots = []
        valid_relations = []
        seen_evaluator_ids = set()
        for relation in relations_by_subject.get(subject.pk, ()):
            evaluator = relation.evaluator
            if relation.relationship_type in counts:
                counts[relation.relationship_type] += 1
            if not evaluator.wecom_userid:
                unbound_employees[evaluator.public_id] = _employee_display(evaluator)
            relation_error = _relationship_error(subject, relation)
            if evaluator.pk in seen_evaluator_ids:
                relation_error = (
                    "DUPLICATE_EVALUATOR",
                    "同一评价人不能重复分配到多个关系组",
                )
            seen_evaluator_ids.add(evaluator.pk)
            if relation_error:
                code, message = relation_error
                anomalies.append(
                    {
                        "code": code,
                        "message": message,
                        "subject_public_id": str(subject.public_id),
                        "evaluator_public_id": str(evaluator.public_id),
                    }
                )
                continue
            valid_groups.add(relation.relationship_type)
            valid_relations.append(relation)
            relation_snapshots.append(
                _relationship_snapshot(project_subject, relation)
            )

        for group in RULE_GROUPS:
            if group not in valid_groups:
                anomalies.append(
                    {
                        "code": "MISSING_REQUIRED_GROUP",
                        "message": (
                            f"{subject.name}缺少"
                            f"{EvaluationRelationship.Type(group).label}关系"
                        ),
                        "subject_public_id": str(subject.public_id),
                        "relationship_type": group,
                    }
                )

        plans.append(
            _SubjectPlan(
                project_subject=project_subject,
                subject_snapshot=_employee_display(subject),
                template_snapshot=_template_snapshot(
                    project_subject, cached_template.snapshot_payload
                ),
                relationship_snapshot=relation_snapshots,
                relations=tuple(valid_relations),
            )
        )

    preview = ProjectPreview(
        total_count=sum(counts.values()),
        counts_by_group=counts,
        unbound_employees=tuple(
            unbound_employees[key]
            for key in sorted(unbound_employees, key=lambda item: str(item))
        ),
        relation_anomalies=tuple(anomalies),
    )
    return _Analysis(preview=preview, subject_plans=tuple(plans))


def _relations_by_subject(project_subjects):
    subject_ids = [item.subject_id for item in project_subjects]
    relations = (
        EvaluationRelationship.objects.filter(
            subject_id__in=subject_ids, is_active=True
        )
        .select_related("evaluator")
        .order_by("subject_id", "relationship_type", "evaluator__public_id")
    )
    result = {}
    for relation in relations:
        result.setdefault(relation.subject_id, []).append(relation)
    return result


def _build_template_cache(project_subjects):
    templates = {}
    categories = {}
    for project_subject in project_subjects:
        templates[project_subject.template_id] = project_subject.template
        categories[project_subject.template_id] = project_subject.subject.category
    items_by_template = {template_id: [] for template_id in templates}
    if templates:
        for item in TemplateItem.objects.filter(
            template_id__in=templates
        ).order_by("template_id", "order", "pk"):
            items_by_template[item.template_id].append(item)
    cache = {}
    for template_id, template in templates.items():
        items = items_by_template[template_id]
        category = categories[template_id]
        cache[template_id] = _TemplateCacheEntry(
            validation_errors=tuple(validate_template_items(items)),
            snapshot_payload={
                "public_id": str(template.public_id),
                "name": template.name,
                "version": template.version,
                "category": {
                    "public_id": str(category.public_id),
                    "name": category.name,
                },
                "items": [
                    {
                        "group": item.group,
                        "title": item.title,
                        "order": item.order,
                        "weight": str(item.weight),
                        "score_min": item.score_min,
                        "score_max": item.score_max,
                        "excellent_description": item.excellent_description,
                        "good_description": item.good_description,
                        "qualified_description": item.qualified_description,
                        "improvement_description": item.improvement_description,
                    }
                    for item in items
                ],
            },
        )
    return cache


def _validate_subject_and_template(subject, template, template_validation_errors):
    errors = []
    if not subject.is_active:
        errors.append(("INACTIVE_SUBJECT", "被评价人已停用"))
    if not subject.category.is_active:
        errors.append(("INACTIVE_CATEGORY", "被评价人的员工类别已停用"))
    if template.category_id != subject.category_id:
        errors.append(("TEMPLATE_CATEGORY_MISMATCH", "评价表模板与员工类别不匹配"))
    if not template.is_active:
        errors.append(("INACTIVE_TEMPLATE", "评价表模板已停用"))
    if not template.is_sealed:
        errors.append(("UNSEALED_TEMPLATE", "评价表模板尚未密封"))
    for message in template_validation_errors:
        errors.append(("INVALID_TEMPLATE", message))
    return errors


def _relationship_error(subject, relation):
    if not relation.evaluator.is_active:
        return "INACTIVE_EVALUATOR", "评价人已停用"
    try:
        validate_relationship_entities(
            subject, relation.evaluator, relation.relationship_type
        )
    except RosterValidationError as exc:
        return exc.code, str(exc)
    return None


def _employee_display(employee):
    return {
        "public_id": str(employee.public_id),
        "name": employee.name,
        "department_level_1": employee.department_level_1,
        "department_level_2": employee.department_level_2,
    }


def _reporting_display(employee, relations):
    manager_name = next(
        (
            relation.evaluator.name
            for relation in relations
            if relation.relationship_type == EvaluationRelationship.Type.MANAGER
        ),
        "",
    )
    return {
        "employee_no": employee.employee_no,
        "corporate_email": employee.corporate_email,
        "department": f"{employee.department_level_1}/{employee.department_level_2}",
        "manager_name": manager_name,
    }


def _unbound_employee_payloads(employees):
    return [
        _employee_display(employee)
        for employee in employees.order_by("public_id")
        if not employee.wecom_userid
    ]


def _template_snapshot(project_subject, snapshot_payload):
    return {
        "public_id": snapshot_payload["public_id"],
        "name": snapshot_payload["name"],
        "version": snapshot_payload["version"],
        "category": dict(snapshot_payload["category"]),
        "items": [
            {
                "snapshot_item_id": str(
                    uuid5(
                        project_subject.public_id,
                        f"template-item:{item['order']}",
                    )
                ),
                **item,
            }
            for item in snapshot_payload["items"]
        ],
    }


def _relationship_snapshot(project_subject, relation):
    return {
        "snapshot_item_id": str(
            uuid5(
                project_subject.public_id,
                (
                    f"relationship:{relation.evaluator.public_id}:"
                    f"{relation.relationship_type}"
                ),
            )
        ),
        "relationship_type": relation.relationship_type,
        "evaluator": _employee_display(relation.evaluator),
    }


def prepare_project(project, actor):
    require_hr_actor(actor)
    from apps.reporting.services.summary import verify_project_template_binding

    try:
        with transaction.atomic():
            locked_project = EvaluationProject.objects.select_for_update(
                of=("self",)
            ).get(pk=project.pk)
            if locked_project.status == EvaluationProject.Status.READY:
                project_subjects = list(
                    ProjectSubject.objects.select_for_update(of=("self",))
                    .filter(project=locked_project)
                    .order_by("pk")
                )
                tasks = list(
                    EvaluationTask.objects.select_for_update(of=("self",))
                    .filter(project=locked_project)
                    .order_by("pk")
                )
                total_count = validate_ready_project_integrity(
                    locked_project, project_subjects, tasks
                )
                result = PrepareResult(
                    created_count=0,
                    total_count=total_count,
                )
                _sync_project(project, locked_project)
                return result
            if locked_project.status != EvaluationProject.Status.DRAFT:
                raise ProjectStateError(
                    "只有草稿项目可以准备", "PROJECT_NOT_DRAFT"
                )
            if locked_project.tasks.exists():
                raise ProjectStateError(
                    "草稿项目存在异常任务，请重新创建项目",
                    "DRAFT_HAS_TASKS",
                )
            verify_project_template_binding(locked_project)

            project_subjects = list(
                ProjectSubject.objects.select_for_update(of=("self",))
                .filter(project=locked_project)
                .order_by("pk")
            )
            _lock_preflight_dependencies(project_subjects)
            project_subjects = list(
                ProjectSubject.objects.filter(project=locked_project)
                .select_related("subject__category", "template")
                .order_by("pk")
            )
            analysis = _analyze_project(locked_project, project_subjects)
            if analysis.preview.relation_anomalies:
                raise ProjectValidationError(
                    [
                        anomaly["message"]
                        for anomaly in analysis.preview.relation_anomalies
                    ]
                )
            normalized_rules = normalize_project_rules(locked_project.rule_snapshot)

            tasks = []
            for plan in analysis.subject_plans:
                plan.project_subject.subject_snapshot = plan.subject_snapshot
                plan.project_subject.template_snapshot = plan.template_snapshot
                plan.project_subject.relationship_snapshot = plan.relationship_snapshot
                plan.project_subject.reporting_snapshot = _reporting_display(
                    plan.project_subject.subject,
                    plan.relations,
                )
                snapshots_by_relation = {
                    (
                        item["evaluator"]["public_id"],
                        item["relationship_type"],
                    ): item
                    for item in plan.relationship_snapshot
                }
                for relation in plan.relations:
                    snapshot = snapshots_by_relation[
                        (str(relation.evaluator.public_id), relation.relationship_type)
                    ]
                    tasks.append(
                        EvaluationTask(
                            project=locked_project,
                            project_subject=plan.project_subject,
                            evaluator=relation.evaluator,
                            subject=plan.project_subject.subject,
                            relationship_type=relation.relationship_type,
                            relationship_snapshot_item_id=snapshot["snapshot_item_id"],
                        )
                    )

            ProjectSubject.objects.bulk_update(
                [plan.project_subject for plan in analysis.subject_plans],
                [
                    "subject_snapshot",
                    "template_snapshot",
                    "relationship_snapshot",
                    "reporting_snapshot",
                ],
            )
            try:
                validate_task_associations(tasks)
            except DjangoValidationError as exc:
                raise ProjectStateError(
                    "项目任务关联不一致",
                    "PROJECT_TASK_ASSOCIATION_INVALID",
                ) from exc
            EvaluationTask.objects.bulk_create(tasks)
            locked_project.rule_snapshot = normalized_rules
            locked_project.reporting_snapshot = {"name": locked_project.name}
            locked_project.status = EvaluationProject.Status.READY
            locked_project.prepared_at = timezone.now()
            locked_project.save(
                update_fields=[
                    "rule_snapshot",
                    "reporting_snapshot",
                    "status",
                    "prepared_at",
                ]
            )
            result = PrepareResult(created_count=len(tasks), total_count=len(tasks))
            record_audit(
                actor,
                "PROJECT_PREPARED",
                locked_project,
                {"created_task_count": len(tasks), "total_task_count": len(tasks)},
                idempotency_key=f"project:{locked_project.public_id}:prepared",
            )
            _sync_project(project, locked_project)
            return result
    except IntegrityError as exc:
        raise ProjectStateError(
            "项目准备发生并发冲突，请刷新后重试", "PROJECT_PREPARE_CONFLICT"
        ) from exc
    except OperationalError as exc:
        if not _is_retryable_prepare_operational_error(exc):
            raise
        raise ProjectStateError(
            "项目准备遇到数据库锁冲突，请重试", "PROJECT_PREPARE_RETRY"
        ) from exc


def _operational_error_sqlstate(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            try:
                value = getattr(current, attribute, None)
            except Exception:
                value = None
            if isinstance(value, str) and value:
                return value
        current = getattr(current, "__cause__", None)
    return None


def _is_retryable_prepare_operational_error(error):
    if _operational_error_sqlstate(error) in RETRYABLE_POSTGRESQL_SQLSTATES:
        return True
    return (
        connection.vendor == "sqlite"
        and "database is locked" in str(error).lower()
    )


def _lock_preflight_dependencies(project_subjects):
    """Lock shared mutable rows in one global model/PK order.

    Project and ProjectSubject rows are project-local and are locked by the
    caller first. Shared rows follow SHARED_MUTABLE_DEPENDENCY_LOCK_ORDER:
    Employee -> EmployeeCategory -> EvaluationRelationship. FormTemplate and
    TemplateItem are sealed immutable
    versions, so preparation reads them without taking shared row locks.
    """
    _employee_model, category_model, relationship_model = (
        roster_locking.SHARED_MUTABLE_DEPENDENCY_LOCK_ORDER
    )
    subject_ids = sorted({item.subject_id for item in project_subjects})
    relation_metadata = list(
        relationship_model.objects.filter(subject_id__in=subject_ids)
        .order_by("pk")
        .values_list("pk", "evaluator_id")
    )
    employee_ids = sorted(
        set(subject_ids) | {evaluator_id for _, evaluator_id in relation_metadata}
    )
    locked_employees = roster_locking.lock_employee_rows(employee_ids)
    category_ids = sorted(
        {
            employee.category_id
            for employee in locked_employees
            if employee.pk in subject_ids
        }
    )
    list(
        category_model.objects.select_for_update(of=("self",))
        .filter(pk__in=category_ids)
        .order_by("pk")
    )
    relation_ids = [relation_id for relation_id, _ in relation_metadata]
    list(
        relationship_model.objects.select_for_update(of=("self",))
        .filter(pk__in=relation_ids)
        .order_by("pk")
    )


def _sync_project(target, source):
    for field in (
        "status",
        "deadline",
        "rule_snapshot",
        "reporting_snapshot",
        "prepared_at",
        "launched_at",
    ):
        setattr(target, field, getattr(source, field))


def extend_project_deadline(project, deadline, actor):
    require_hr_actor(actor)
    with transaction.atomic():
        locked_project = EvaluationProject.objects.select_for_update(
            of=("self",)
        ).get(pk=project.pk)
        if locked_project.status not in (
            EvaluationProject.Status.READY,
            EvaluationProject.Status.ACTIVE,
        ):
            raise ProjectStateError(
                "只有待发送或进行中的项目可以延长截止时间",
                "DEADLINE_STATE_INVALID",
            )
        if deadline <= locked_project.deadline:
            raise ProjectStateError(
                "截止时间只能向后延长", "DEADLINE_NOT_LATER"
            )
        locked_project.deadline = deadline
        locked_project.save(update_fields=["deadline"])
        record_audit(
            actor,
            "PROJECT_DEADLINE_EXTENDED",
            locked_project,
            {"fields": ["deadline"]},
        )
        _sync_project(project, locked_project)
        return locked_project


def close_project_early(project, actor):
    require_hr_actor(actor)
    with transaction.atomic():
        locked_project = EvaluationProject.objects.select_for_update(
            of=("self",)
        ).get(pk=project.pk)
        if locked_project.status == EvaluationProject.Status.CLOSED:
            _sync_project(project, locked_project)
            return locked_project
        if locked_project.status != EvaluationProject.Status.ACTIVE:
            raise ProjectStateError(
                "只有进行中的项目可以提前截止", "CLOSE_STATE_INVALID"
            )
        locked_project.tasks.filter(status=EvaluationTask.Status.PENDING).update(
            status=EvaluationTask.Status.NOT_SUBMITTED
        )
        locked_project.status = EvaluationProject.Status.CLOSED
        locked_project.save(update_fields=["status"])
        record_audit(
            actor,
            "PROJECT_CLOSED",
            locked_project,
            {"fields": ["status"]},
            idempotency_key=f"project:{locked_project.public_id}:closed",
        )
        _sync_project(project, locked_project)
        return locked_project
