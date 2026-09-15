from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction

from apps.audit.services import record_audit
from apps.core.permissions import require_hr_actor
from apps.roster.models import EmployeeCategory

from ..models.templates import (
    FormTemplate,
    ITEM_PAYLOAD_FIELDS,
    TemplateItem,
)


class TemplateValidationError(ValueError):
    def __init__(self, errors, code="INVALID"):
        self.errors = errors
        self.code = code
        super().__init__("；".join(errors))


def validate_template(template):
    return validate_template_items(list(template.items.all()))


def validate_template_items(items):
    items = list(items)
    if not items:
        return ["评价表至少需要一个评价项"]

    errors = []
    orders = [item.order for item in items]
    if len(orders) != len(set(orders)):
        errors.append("评价项序号必须唯一")
    if any(not item.title.strip() for item in items):
        errors.append("评价项标题不能为空")
    if any(
        not 1 <= item.score_min <= item.score_max <= 5
        for item in items
    ):
        errors.append("评价项评分范围无效")
    descriptions = (
        "excellent_description",
        "good_description",
        "qualified_description",
        "improvement_description",
    )
    if any(
        not getattr(item, description).strip()
        for item in items
        for description in descriptions
    ):
        errors.append("评价项四档描述均为必填项")
    total_weight = sum((item.weight for item in items), Decimal("0.00000"))
    if total_weight != Decimal("1.00000"):
        errors.append("评价项权重合计必须为 100%")
    return errors


def create_initial_template(*, category, name, items, actor, mutation_key=None):
    require_hr_actor(actor)
    with transaction.atomic():
        category = _lock_category(category)
        if _lock_template_chain(category):
            raise TemplateValidationError(["该员工类别已有评价表模板"])
        template = _build_and_seal_template(
            category=category,
            name=name,
            version=1,
            previous_version=None,
            item_payloads=items,
            actor=actor,
        )
        record_audit(
            actor,
            "TEMPLATE_CREATED",
            template,
            {"version": template.version},
            idempotency_key=(
                f"template-create:{actor.public_id}:{mutation_key}"
                if mutation_key
                else f"template:{template.public_id}:created"
            ),
        )
        record_audit(
            actor,
            "TEMPLATE_SEALED",
            template,
            {"fields": ["is_sealed"]},
            idempotency_key=f"template:{template.public_id}:sealed",
        )
        return template


def create_template_version(source, changes, actor, mutation_key=None):
    """Copy a saved template into the next immutable version under a category lock."""
    require_hr_actor(actor)
    with transaction.atomic():
        if source.pk is None or source.category_id is None:
            raise TemplateValidationError(["评价表模板不存在"], "TEMPLATE_NOT_FOUND")
        category = _lock_category(source.category_id)
        locked_templates = _lock_template_chain(category)
        source = next(
            (template for template in locked_templates if template.pk == source.pk), None
        )
        if source is None:
            raise TemplateValidationError(["评价表模板不存在"], "TEMPLATE_NOT_FOUND")
        if not source.is_sealed:
            raise TemplateValidationError(["评价表模板尚未密封"], "TEMPLATE_UNSEALED")
        if not locked_templates:
            raise TemplateValidationError(["评价表模板不存在"], "TEMPLATE_NOT_FOUND")
        latest = max(locked_templates, key=lambda template: template.version)
        if latest.pk != source.pk:
            raise TemplateValidationError(
                ["只能从最新评价表模板版本创建新版本"], "STALE_TEMPLATE_VERSION"
            )
        latest_version = latest.version
        template = _build_and_seal_template(
            category=category,
            name=changes.get("name", source.name),
            version=latest_version + 1,
            previous_version=source,
            item_payloads=changes.get("items", source.item_payloads()),
            actor=actor,
        )
        record_audit(
            actor,
            "TEMPLATE_VERSION_CREATED",
            template,
            {"version": template.version},
            idempotency_key=(
                f"template-version:{actor.public_id}:{mutation_key}"
                if mutation_key
                else f"template:{template.public_id}:version-created"
            ),
        )
        record_audit(
            actor,
            "TEMPLATE_SEALED",
            template,
            {"fields": ["is_sealed"]},
            idempotency_key=f"template:{template.public_id}:sealed",
        )
        return template


def _lock_category(category):
    return EmployeeCategory.objects.select_for_update(of=("self",)).get(
        pk=getattr(category, "pk", category)
    )


def _lock_template_chain(category):
    """Lock every category version in primary-key order after locking its category."""
    return list(
        FormTemplate.objects.select_for_update(of=("self",))
        .filter(category=category)
        .order_by("pk")
    )


def _require_authorized_atomic_service(actor):
    require_hr_actor(actor)
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("评价表模板服务必须在事务中构建")


def _build_and_seal_template(
    *, category, name, version, previous_version, item_payloads, actor
):
    """Atomically persist, validate, and seal one complete template candidate."""
    _require_authorized_atomic_service(actor)
    try:
        with transaction.atomic():
            template = FormTemplate(
                category=category,
                name=name.strip(),
                version=version,
                previous_version=previous_version,
                is_sealed=False,
                created_by=actor,
            )
            models.Model.save(
                template, force_insert=True, using=FormTemplate.objects.db
            )
            TemplateItem.objects.bulk_create(
                [
                    TemplateItem(
                        template=template,
                        **{field: payload[field] for field in ITEM_PAYLOAD_FIELDS},
                    )
                    for payload in item_payloads
                ]
            )
            locked_template = FormTemplate.objects.select_for_update(of=("self",)).get(
                pk=template.pk
            )
            errors = validate_template(locked_template)
            if errors:
                raise TemplateValidationError(errors)
            locked_template.is_sealed = True
            try:
                models.Model.save(
                    locked_template,
                    update_fields={"is_sealed"},
                    using=FormTemplate.objects.db,
                )
            except ValidationError as exc:
                raise TemplateValidationError(
                    ["评价表模板密封失败"], "TEMPLATE_SEAL_FAILED"
                ) from exc
            locked_template.refresh_from_db()
            if not locked_template.is_sealed:
                raise TemplateValidationError(
                    ["评价表模板密封失败"], "TEMPLATE_SEAL_FAILED"
                )
            return locked_template
    except IntegrityError as exc:
        _raise_template_integrity_error(exc)


def _raise_template_integrity_error(error):
    message = str(error).lower()
    if (
        "unique_template_item_order" in message
        or "evaluations_templateitem.template_id, evaluations_templateitem.order" in message
    ):
        raise TemplateValidationError(
            ["评价项序号必须唯一"], "ITEM_ORDER_CONFLICT"
        ) from error
    if (
        "unique_category_template_version" in message
        or "evaluations_formtemplate.category_id, evaluations_formtemplate.version" in message
    ):
        raise TemplateValidationError(
            ["评价表模板版本冲突，请重试"], "TEMPLATE_VERSION_CONFLICT"
        ) from error
    raise TemplateValidationError(["评价表模板保存冲突，请重试"], "TEMPLATE_INTEGRITY") from error
