from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.audit.services import record_audit
from apps.core.permissions import HRPermissionError, require_hr_actor as require_hr_permission

from . import locking as roster_locking
from .models import Employee, EmployeeCategory, EvaluationRelationship


DEFAULT_GROUPS = ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR")


def ensure_default_groups() -> None:
    for name in DEFAULT_GROUPS:
        Group.objects.get_or_create(name=name)


class RosterValidationError(ValueError):
    def __init__(self, message: str, code: str = "INVALID"):
        super().__init__(message)
        self.code = code


def require_hr_actor(actor) -> None:
    try:
        require_hr_permission(actor)
    except HRPermissionError as exc:
        raise RosterValidationError(str(exc), exc.code) from exc


def _required(value, label: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RosterValidationError(f"{label}不能为空", "REQUIRED")
    if len(normalized) > max_length:
        raise RosterValidationError(f"{label}长度超限", "MAX_LENGTH")
    return normalized


def normalize_employee_fields(
    *,
    employee_no,
    name,
    corporate_email,
    department_level_1,
    department_level_2,
    wecom_userid=None,
) -> dict:
    values = {
        "employee_no": _required(employee_no, "员工编号", 40),
        "name": _required(name, "姓名", 100),
        "corporate_email": _required(corporate_email, "企业邮箱", 254).lower(),
        "department_level_1": _required(department_level_1, "一级部门", 120),
        "department_level_2": _required(department_level_2, "二级部门", 120),
        "wecom_userid": str(wecom_userid or "").strip() or None,
    }
    if values["wecom_userid"] and len(values["wecom_userid"]) > 128:
        raise RosterValidationError("企业微信 UserId 长度超限", "MAX_LENGTH")
    try:
        validate_email(values["corporate_email"])
    except ValidationError as exc:
        raise RosterValidationError("企业邮箱格式不正确", "INVALID_EMAIL") from exc
    return values


def validate_employee_data(
    *,
    employee_no,
    name,
    corporate_email,
    department_level_1,
    department_level_2,
    category_id,
    wecom_userid=None,
    instance=None,
) -> dict:
    values = normalize_employee_fields(
        employee_no=employee_no,
        name=name,
        corporate_email=corporate_email,
        department_level_1=department_level_1,
        department_level_2=department_level_2,
        wecom_userid=wecom_userid,
    )

    try:
        category = EmployeeCategory.objects.get(pk=category_id)
    except (EmployeeCategory.DoesNotExist, TypeError, ValueError) as exc:
        raise RosterValidationError("员工类别不存在", "CATEGORY_NOT_FOUND") from exc
    if not category.is_active:
        raise RosterValidationError("员工类别已停用", "CATEGORY_INACTIVE")
    values["category"] = category

    unique_fields = (
        ("employee_no", "员工编号已存在"),
        ("corporate_email", "企业邮箱已存在"),
        ("wecom_userid", "企业微信 UserId 已存在"),
    )
    for field, message in unique_fields:
        value = values[field]
        if value is None:
            continue
        matches = Employee.objects.filter(**{field: value})
        if instance is not None:
            matches = matches.exclude(pk=instance.pk)
        if matches.exists():
            raise RosterValidationError(message, f"DUPLICATE_{field.upper()}")
    return values


def _raise_employee_integrity_error(values, instance, error):
    for field, message in (
        ("employee_no", "员工编号已存在"),
        ("corporate_email", "企业邮箱已存在"),
        ("wecom_userid", "企业微信 UserId 已存在"),
    ):
        value = values.get(field)
        if value is None:
            continue
        matches = Employee.objects.filter(**{field: value})
        if instance is not None:
            matches = matches.exclude(pk=instance.pk)
        if matches.first() is not None:
            raise RosterValidationError(
                message, f"DUPLICATE_{field.upper()}"
            ) from error
    raise RosterValidationError("员工唯一字段冲突", "EMPLOYEE_INTEGRITY") from error


def _lock_employee(employee_id):
    locked = roster_locking.lock_employee_rows([employee_id])
    if not locked:
        raise Employee.DoesNotExist
    return locked[0]


@transaction.atomic
def create_employee_record(*, actor, **fields) -> Employee:
    require_hr_actor(actor)
    values = validate_employee_data(**fields)
    try:
        with transaction.atomic():
            employee = Employee.objects.create(**values)
    except IntegrityError as exc:
        _raise_employee_integrity_error(values, None, exc)
    record_audit(
        actor,
        "EMPLOYEE_CREATED",
        employee,
        {"fields": sorted(values)},
        idempotency_key=f"employee:{employee.public_id}:created",
    )
    return employee


@transaction.atomic
def update_employee_record(employee_id, *, actor, **fields) -> Employee:
    require_hr_actor(actor)
    employee = _lock_employee(employee_id)
    values = validate_employee_data(instance=employee, **fields)
    changed_fields = sorted(
        field for field, value in values.items() if getattr(employee, field) != value
    )
    for field, value in values.items():
        setattr(employee, field, value)
    try:
        with transaction.atomic():
            employee.save(update_fields=[*values.keys()])
    except IntegrityError as exc:
        _raise_employee_integrity_error(values, employee, exc)
    if changed_fields:
        record_audit(
            actor,
            "EMPLOYEE_UPDATED",
            employee,
            {"fields": changed_fields},
        )
    return employee


@transaction.atomic
def deactivate_employee(employee_id, *, actor) -> Employee:
    require_hr_actor(actor)
    employee = _lock_employee(employee_id)
    if employee.is_active:
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        record_audit(
            actor,
            "EMPLOYEE_DEACTIVATED",
            employee,
            {"fields": ["is_active"]},
            idempotency_key=f"employee:{employee.public_id}:deactivated",
        )
    return employee


def _validate_category_values(code, name, instance=None) -> tuple[str, str]:
    code = _required(code, "类别编码", 40)
    name = _required(name, "类别名称", 100)
    for field, value, message in (
        ("code", code, "类别编码已存在"),
        ("name", name, "类别名称已存在"),
    ):
        matches = EmployeeCategory.objects.filter(**{field: value})
        if instance is not None:
            matches = matches.exclude(pk=instance.pk)
        if matches.exists():
            raise RosterValidationError(message, f"DUPLICATE_CATEGORY_{field.upper()}")
    return code, name


def _raise_category_integrity_error(code, name, instance, error):
    for field, value, message in (
        ("code", code, "类别编码已存在"),
        ("name", name, "类别名称已存在"),
    ):
        matches = EmployeeCategory.objects.filter(**{field: value})
        if instance is not None:
            matches = matches.exclude(pk=instance.pk)
        if matches.first() is not None:
            raise RosterValidationError(
                message, f"DUPLICATE_CATEGORY_{field.upper()}"
            ) from error
    raise RosterValidationError("类别唯一字段冲突", "CATEGORY_INTEGRITY") from error


@transaction.atomic
def create_category_record(*, code, name, actor) -> EmployeeCategory:
    require_hr_actor(actor)
    code, name = _validate_category_values(code, name)
    try:
        with transaction.atomic():
            category = EmployeeCategory.objects.create(code=code, name=name)
    except IntegrityError as exc:
        _raise_category_integrity_error(code, name, None, exc)
    record_audit(
        actor,
        "CATEGORY_CREATED",
        category,
        {"fields": ["code", "name"]},
        idempotency_key=f"category:{category.public_id}:created",
    )
    return category


@transaction.atomic
def update_category_record(category_id, *, code, name, actor) -> EmployeeCategory:
    require_hr_actor(actor)
    category = EmployeeCategory.objects.select_for_update(of=("self",)).get(
        pk=category_id
    )
    code, name = _validate_category_values(code, name, category)
    changed_fields = [
        field
        for field, value in (("code", code), ("name", name))
        if getattr(category, field) != value
    ]
    category.code = code
    category.name = name
    try:
        with transaction.atomic():
            category.save(update_fields=["code", "name"])
    except IntegrityError as exc:
        _raise_category_integrity_error(code, name, category, exc)
    if changed_fields:
        record_audit(
            actor,
            "CATEGORY_UPDATED",
            category,
            {"fields": changed_fields},
        )
    return category


@transaction.atomic
def deactivate_category(category_id, *, actor) -> EmployeeCategory:
    require_hr_actor(actor)
    category = EmployeeCategory.objects.select_for_update(of=("self",)).get(
        pk=category_id
    )
    if category.is_active:
        category.is_active = False
        category.save(update_fields=["is_active"])
        record_audit(
            actor,
            "CATEGORY_DEACTIVATED",
            category,
            {"fields": ["is_active"]},
            idempotency_key=f"category:{category.public_id}:deactivated",
        )
    return category


def validate_relationship_entities(subject, evaluator, relationship_type):
    if relationship_type not in EvaluationRelationship.Type.values:
        raise RosterValidationError("关系类型无效", "INVALID_RELATIONSHIP_TYPE")
    if subject.pk == evaluator.pk:
        raise RosterValidationError("不能建立本人评价关系", "SELF_RELATION")
    same_department = (
        subject.department_level_1 == evaluator.department_level_1
        and subject.department_level_2 == evaluator.department_level_2
    )
    if relationship_type == EvaluationRelationship.Type.SAME_DEPARTMENT and not same_department:
        raise RosterValidationError("同部门协作关系要求部门一致", "SAME_DEPARTMENT_REQUIRED")
    if relationship_type == EvaluationRelationship.Type.CROSS_DEPARTMENT and same_department:
        raise RosterValidationError("跨部门协作关系要求部门不同", "CROSS_DEPARTMENT_REQUIRED")


def resolve_and_validate_relationship(
    *,
    subject_no,
    evaluator_no,
    relationship_type,
    allow_active_duplicate=False,
    exclude_relationship_id=None,
):
    employee_nos = [str(subject_no or "").strip(), str(evaluator_no or "").strip()]
    employees = {
        employee.employee_no: employee
        for employee in Employee.objects.filter(employee_no__in=employee_nos, is_active=True)
    }
    if any(employee_no not in employees for employee_no in employee_nos):
        raise RosterValidationError("员工编号不存在或已停用", "EMPLOYEE_NOT_FOUND")
    subject, evaluator = (employees[employee_no] for employee_no in employee_nos)
    validate_relationship_entities(subject, evaluator, relationship_type)
    active_matches = EvaluationRelationship.objects.filter(
        subject=subject,
        evaluator=evaluator,
        relationship_type=relationship_type,
        is_active=True,
    )
    if exclude_relationship_id is not None:
        active_matches = active_matches.exclude(pk=exclude_relationship_id)
    active = active_matches.first()
    if active and not allow_active_duplicate:
        raise RosterValidationError("协作关系已存在", "DUPLICATE_RELATION")
    return subject, evaluator, active


def _lock_and_validate_relationship_employees(
    subject_no, evaluator_no, relationship_type, *, extra_employee_ids=()
):
    """Resolve read-only IDs, then take the shared Employee-first bulk lock."""
    employee_nos = [str(subject_no or "").strip(), str(evaluator_no or "").strip()]
    employee_ids_by_no = dict(
        Employee.objects.filter(
            employee_no__in=employee_nos, is_active=True
        ).values_list("employee_no", "pk")
    )
    if any(employee_no not in employee_ids_by_no for employee_no in employee_nos):
        raise RosterValidationError(
            "员工编号不存在或已停用", "EMPLOYEE_NOT_FOUND"
        )
    locked_employees = roster_locking.lock_employee_rows(
        [*employee_ids_by_no.values(), *extra_employee_ids]
    )
    employees_by_no = {
        employee.employee_no: employee
        for employee in locked_employees
        if employee.is_active
    }
    if any(employee_no not in employees_by_no for employee_no in employee_nos):
        raise RosterValidationError(
            "员工编号不存在或已停用", "EMPLOYEE_NOT_FOUND"
        )
    subject, evaluator = (employees_by_no[number] for number in employee_nos)
    validate_relationship_entities(subject, evaluator, relationship_type)
    return subject, evaluator


@transaction.atomic
def upsert_relationship(*, subject_no, evaluator_no, relationship_type, actor):
    require_hr_actor(actor)
    subject, evaluator = _lock_and_validate_relationship_employees(
        subject_no, evaluator_no, relationship_type
    )
    matching = list(
        EvaluationRelationship.objects.select_for_update(of=("self",))
        .filter(
            subject=subject,
            evaluator=evaluator,
            relationship_type=relationship_type,
        )
        .order_by("pk")
    )
    if any(relationship.is_active for relationship in matching):
        raise RosterValidationError(
            "协作关系已存在", "DUPLICATE_RELATION"
        )
    inactive = matching[0] if matching else None
    if inactive:
        inactive.is_active = True
        try:
            with transaction.atomic():
                inactive.save(update_fields=["is_active"])
        except IntegrityError as exc:
            raise RosterValidationError(
                "协作关系已存在", "DUPLICATE_RELATION"
            ) from exc
        record_audit(
            actor,
            "RELATIONSHIP_CREATED",
            inactive,
            {"operation": "reactivated"},
        )
        return inactive
    try:
        with transaction.atomic():
            relationship = EvaluationRelationship.objects.create(
                subject=subject,
                evaluator=evaluator,
                relationship_type=relationship_type,
            )
    except IntegrityError as exc:
        if EvaluationRelationship.objects.filter(
            subject=subject,
            evaluator=evaluator,
            relationship_type=relationship_type,
            is_active=True,
        ).first():
            raise RosterValidationError(
                "协作关系已存在", "DUPLICATE_RELATION"
            ) from exc
        raise RosterValidationError(
            "协作关系唯一性冲突", "RELATIONSHIP_INTEGRITY"
        ) from exc
    record_audit(
        actor,
        "RELATIONSHIP_CREATED",
        relationship,
        {"fields": ["subject", "evaluator", "relationship_type"]},
        idempotency_key=f"relationship:{relationship.public_id}:created",
    )
    return relationship


@transaction.atomic
def deactivate_relationship(relationship_id, *, actor):
    require_hr_actor(actor)
    relationship = EvaluationRelationship.objects.select_for_update(of=("self",)).get(
        pk=relationship_id
    )
    if relationship.is_active:
        relationship.is_active = False
        relationship.save(update_fields=["is_active"])
        record_audit(
            actor,
            "RELATIONSHIP_DEACTIVATED",
            relationship,
            {"fields": ["is_active"]},
        )
    return relationship


@transaction.atomic
def update_relationship_record(
    relationship_id, *, subject_no, evaluator_no, relationship_type, actor
):
    require_hr_actor(actor)
    original_links = EvaluationRelationship.objects.filter(
        pk=relationship_id
    ).values("subject_id", "evaluator_id").get()
    subject, evaluator = _lock_and_validate_relationship_employees(
        subject_no,
        evaluator_no,
        relationship_type,
        extra_employee_ids=(
            original_links["subject_id"],
            original_links["evaluator_id"],
        ),
    )
    matching = list(
        EvaluationRelationship.objects.select_for_update(of=("self",))
        .filter(
            Q(pk=relationship_id)
            | Q(
                subject=subject,
                evaluator=evaluator,
                relationship_type=relationship_type,
            )
        )
        .order_by("pk")
    )
    relationship = next(
        (item for item in matching if item.pk == relationship_id), None
    )
    if relationship is None:
        raise EvaluationRelationship.DoesNotExist
    if (
        relationship.subject_id != original_links["subject_id"]
        or relationship.evaluator_id != original_links["evaluator_id"]
    ):
        raise RosterValidationError(
            "协作关系已发生变化，请重试", "RELATIONSHIP_CHANGED"
        )
    if any(
        item.pk != relationship.pk and item.is_active for item in matching
    ):
        raise RosterValidationError("协作关系已存在", "DUPLICATE_RELATION")
    changed_fields = [
        field
        for field, value in (
            ("subject_id", subject.pk),
            ("evaluator_id", evaluator.pk),
            ("relationship_type", relationship_type),
        )
        if getattr(relationship, field) != value
    ]
    relationship.subject = subject
    relationship.evaluator = evaluator
    relationship.relationship_type = relationship_type
    try:
        with transaction.atomic():
            relationship.save(
                update_fields=["subject", "evaluator", "relationship_type"]
            )
    except IntegrityError as exc:
        raise RosterValidationError(
            "协作关系已存在", "DUPLICATE_RELATION"
        ) from exc
    if changed_fields:
        record_audit(
            actor,
            "RELATIONSHIP_UPDATED",
            relationship,
            {"fields": changed_fields},
        )
    return relationship
