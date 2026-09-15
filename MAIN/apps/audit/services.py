from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
import json
import re
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from .models import AuditLog


ROLE_ORDER = ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR")
MAX_SUMMARY_BYTES = 8192
MAX_SUMMARY_DEPTH = 8
MAX_SUMMARY_NODES = 256
MAX_SUMMARY_STRING = 2048
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "magic_link",
    "magic-link",
    "score",
    "answer",
    "response_text",
    "rating",
    "url",
)
_CORRELATION_ID = ContextVar("audit_correlation_id", default=None)


class AuditContractError(ValueError):
    pass


class AuditQueryError(ValueError):
    pass


def set_correlation_id(value):
    return _CORRELATION_ID.set(value)


def reset_correlation_id(token):
    _CORRELATION_ID.reset(token)


@dataclass(frozen=True)
class AuditActionRule:
    allowed_roles: tuple[str, ...]
    target_type: str


def _rule(roles, target_type):
    return AuditActionRule(tuple(roles), target_type)


HR = ("HR_ADMIN", "HR_OPERATOR")
ALL_AUTHENTICATED = ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR")
ADMIN = ("HR_ADMIN",)

AUDIT_ACTION_RULES = {
    "AUTH_LOGIN_SUCCEEDED": _rule(ALL_AUTHENTICATED, "accounts.user"),
    "AUTH_ACCESS_DENIED": _rule(ALL_AUTHENTICATED, "accounts.user"),
    "EMPLOYEE_CREATED": _rule(HR, "roster.employee"),
    "EMPLOYEE_UPDATED": _rule(HR, "roster.employee"),
    "EMPLOYEE_DEACTIVATED": _rule(HR, "roster.employee"),
    "ROSTER_IMPORT_COMMITTED": _rule(HR, "roster.importbatch"),
    "CATEGORY_CREATED": _rule(HR, "roster.employeecategory"),
    "CATEGORY_UPDATED": _rule(HR, "roster.employeecategory"),
    "CATEGORY_DEACTIVATED": _rule(HR, "roster.employeecategory"),
    "RELATIONSHIP_CREATED": _rule(HR, "roster.evaluationrelationship"),
    "RELATIONSHIP_UPDATED": _rule(HR, "roster.evaluationrelationship"),
    "RELATIONSHIP_DEACTIVATED": _rule(HR, "roster.evaluationrelationship"),
    "RELATIONSHIP_IMPORT_COMMITTED": _rule(HR, "roster.importbatch"),
    "TEMPLATE_CREATED": _rule(HR, "evaluations.formtemplate"),
    "TEMPLATE_VERSION_CREATED": _rule(HR, "evaluations.formtemplate"),
    "TEMPLATE_SEALED": _rule(HR, "evaluations.formtemplate"),
    "PROJECT_CREATED": _rule(HR, "evaluations.evaluationproject"),
    "PROJECT_PREPARED": _rule(HR, "evaluations.evaluationproject"),
    "PROJECT_LAUNCHED": _rule(HR, "evaluations.evaluationproject"),
    "PROJECT_DEADLINE_EXTENDED": _rule(HR, "evaluations.evaluationproject"),
    "PROJECT_CLOSED": _rule(HR, "evaluations.evaluationproject"),
    "EVALUATION_SUBMITTED": _rule(("EVALUATOR",), "evaluations.evaluationtask"),
    "NOTIFICATION_SENT": _rule(HR, "notifications.notificationattempt"),
    "NOTIFICATION_FAILED": _rule(HR, "notifications.notificationattempt"),
    "NOTIFICATION_RETRIED": _rule(HR, "notifications.notificationattempt"),
    "REPORT_SUMMARY_EXPORTED": _rule(ADMIN, "evaluations.evaluationproject"),
    "REPORT_RAW_EXPORTED": _rule(ADMIN, "evaluations.evaluationproject"),
    "REPORT_EXCEPTION_EXPORTED": _rule(ADMIN, "evaluations.evaluationproject"),
    "PROJECT_RECOMPUTED": _rule(ADMIN, "evaluations.evaluationproject"),
    "AUDIT_EXPORTED": _rule(ADMIN, "accounts.user"),
}


def validate_audit_contract(action, role, target_type):
    rule = AUDIT_ACTION_RULES.get(action)
    if rule is None:
        raise AuditContractError("未知审计动作")
    if role not in rule.allowed_roles:
        raise AuditContractError("角色无权记录该审计动作")
    if target_type != rule.target_type:
        raise AuditContractError("审计对象类型与动作不匹配")
    return rule


def _reload_actor(actor):
    if not actor or not getattr(actor, "pk", None):
        raise AuditContractError("审计操作者无效")
    try:
        fresh = get_user_model().objects.prefetch_related("groups").get(pk=actor.pk)
    except get_user_model().DoesNotExist as exc:
        raise AuditContractError("审计操作者无效") from exc
    if not fresh.is_active:
        raise AuditContractError("审计操作者无效")
    roles = set(fresh.groups.values_list("name", flat=True))
    if "EVALUATOR" not in roles:
        from apps.roster.models import Employee

        if Employee.objects.filter(user=fresh, is_active=True).exists():
            roles.add("EVALUATOR")
    if not roles.intersection(ROLE_ORDER):
        raise AuditContractError("审计操作者角色无效")
    return fresh, roles


def _reload_target(target):
    meta = getattr(target, "_meta", None)
    pk = getattr(target, "pk", None)
    if meta is None or pk is None:
        raise AuditContractError("审计对象无效")
    model = meta.model
    try:
        fresh = model._default_manager.get(pk=pk)
    except model.DoesNotExist as exc:
        raise AuditContractError("审计对象无效") from exc
    return fresh, meta.label_lower


def _public_identifier(instance):
    value = getattr(instance, "public_id", None)
    return str(value if value is not None else instance.pk)


def _project_for_target(target, target_type):
    if target_type == "evaluations.evaluationproject":
        return target
    project_id = getattr(target, "project_id", None)
    if project_id is None:
        return None
    from apps.evaluations.models import EvaluationProject

    try:
        return EvaluationProject.objects.get(pk=project_id)
    except EvaluationProject.DoesNotExist as exc:
        raise AuditContractError("审计对象项目无效") from exc


def _validate_project_scope(target, target_type, project):
    target_project = _project_for_target(target, target_type)
    if project is not None:
        fresh_project, project_type = _reload_target(project)
        if project_type != "evaluations.evaluationproject":
            raise AuditContractError("审计项目范围无效")
        if target_project is None or target_project.pk != fresh_project.pk:
            raise AuditContractError("审计对象不属于指定项目")
        target_project = fresh_project
    return "" if target_project is None else _public_identifier(target_project)


def _sanitize(value, *, depth=0, counter=None):
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_SUMMARY_NODES or depth > MAX_SUMMARY_DEPTH:
        raise AuditContractError("审计摘要超出结构限制")
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 120:
                raise AuditContractError("审计摘要键无效")
            lowered = key.lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize(
                    item, depth=depth + 1, counter=counter
                )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize(item, depth=depth + 1, counter=counter) for item in value
        ]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise AuditContractError("审计摘要数值无效")
        return value
    if isinstance(value, str):
        if len(value) > MAX_SUMMARY_STRING:
            raise AuditContractError("审计摘要文本过长")
        if re.search(r"https?://", value, flags=re.IGNORECASE):
            return "[REDACTED]"
        return value
    raise AuditContractError("审计摘要值类型无效")


def sanitize_changes(changes):
    if not isinstance(changes, Mapping):
        raise AuditContractError("审计摘要必须是对象")
    sanitized = _sanitize(changes)
    encoded = json.dumps(
        sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise AuditContractError("审计摘要过大")
    return sanitized


def _safe_event_identifier(value, label):
    candidate = str(value or uuid4())
    if SAFE_IDENTIFIER.fullmatch(candidate) is None:
        raise AuditContractError(f"{label}无效")
    return candidate


def _same_event(existing, values):
    fields = (
        "actor_id",
        "effective_role",
        "action",
        "target_type",
        "target_id",
        "target_project_id",
        "change_summary",
    )
    return all(getattr(existing, field) == values[field] for field in fields)


@transaction.atomic
def record_audit(
    actor,
    action,
    target,
    changes,
    *,
    idempotency_key=None,
    correlation_id=None,
    project=None,
):
    fresh_actor, current_roles = _reload_actor(actor)
    fresh_target, target_type = _reload_target(target)
    rule = AUDIT_ACTION_RULES.get(action)
    if rule is None:
        raise AuditContractError("未知审计动作")
    effective_role = next(
        (
            role
            for role in ROLE_ORDER
            if role in current_roles and role in rule.allowed_roles
        ),
        None,
    )
    if effective_role is None:
        raise AuditContractError("角色无权记录该审计动作")
    validate_audit_contract(action, effective_role, target_type)
    summary = sanitize_changes(changes)
    idempotency_key = _safe_event_identifier(idempotency_key, "审计幂等键")
    correlation_id = _safe_event_identifier(
        correlation_id or _CORRELATION_ID.get(), "审计关联标识"
    )
    values = {
        "actor_id": fresh_actor.pk,
        "effective_role": effective_role,
        "action": action,
        "target_type": target_type,
        "target_id": _public_identifier(fresh_target),
        "target_project_id": _validate_project_scope(
            fresh_target, target_type, project
        ),
        "change_summary": summary,
    }
    existing = AuditLog.objects.select_for_update().filter(
        idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if not _same_event(existing, values):
            raise AuditContractError("审计幂等键与已有事件冲突")
        return existing
    try:
        with transaction.atomic():
            return AuditLog.objects.create(
                **values,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
    except IntegrityError:
        existing = AuditLog.objects.get(idempotency_key=idempotency_key)
        if not _same_event(existing, values):
            raise AuditContractError("审计幂等键与已有事件冲突")
        return existing


@dataclass(frozen=True)
class RecentActivity:
    event: AuditLog
    url: str


def dashboard_metrics():
    from apps.evaluations.models import (
        AggregateResult,
        EvaluationProject,
        EvaluationTask,
        ProjectSubject,
    )
    from apps.notifications.models import NotificationAttempt, NotificationOutbox
    from apps.roster.models import Employee

    active_status = EvaluationProject.Status.ACTIVE
    active_tasks = EvaluationTask.objects.filter(project__status=active_status)
    completion = active_tasks.aggregate(
        denominator=Count("id"),
        numerator=Count(
            "id", filter=Q(status=EvaluationTask.Status.SUBMITTED)
        ),
        pending=Count("id", filter=Q(status=EvaluationTask.Status.PENDING)),
    )
    latest_attempt_status = NotificationAttempt.objects.filter(
        outbox_id=OuterRef("pk")
    ).order_by("-attempt", "-id").values("status")[:1]
    failed_outboxes = (
        NotificationOutbox.objects.filter(project__status=active_status)
        .annotate(latest_status=Subquery(latest_attempt_status))
        .filter(latest_status=NotificationAttempt.Status.FAILED)
        .count()
    )
    anomalies = (
        ProjectSubject.objects.filter(project__status=active_status)
        .filter(
            Q(
                project__deadline__lte=timezone.now(),
                tasks__status=EvaluationTask.Status.PENDING,
            )
            | Q(aggregate_results__status=AggregateResult.Status.INCOMPLETE)
        )
        .distinct()
        .count()
    )
    return {
        "employee_count": Employee.objects.filter(is_active=True).count(),
        "active_projects": EvaluationProject.objects.filter(
            status=active_status
        ).count(),
        "completion_numerator": completion["numerator"],
        "completion_denominator": completion["denominator"],
        "pending_tasks": completion["pending"],
        "failed_outboxes": failed_outboxes,
        "anomalies": anomalies,
    }


def _activity_url(event, role):
    if event.target_type == "roster.importbatch":
        return reverse("roster:import-preview", kwargs={"batch_id": event.target_id})
    if event.target_type == "evaluations.evaluationproject":
        return reverse(
            "evaluations:project-detail", kwargs={"project_id": event.target_id}
        )
    if event.target_type == "notifications.notificationattempt":
        return reverse(
            "notifications:project-status",
            kwargs={"project_id": event.target_project_id},
        )
    if event.target_type == "evaluations.formtemplate":
        return reverse("evaluations:template-list")
    if event.target_type == "roster.employeecategory":
        return reverse("roster:category-list")
    if event.target_type == "roster.evaluationrelationship":
        return reverse("roster:relationship-list")
    if event.target_type == "roster.employee":
        return reverse("roster:roster-list")
    if role == "HR_ADMIN":
        return reverse("audit:list")
    return reverse("audit:dashboard")


def recent_activity_for_role(role, limit=8):
    if limit < 1 or limit > 8:
        raise AuditContractError("近期活动数量无效")
    if role == "HR_ADMIN":
        visible_actions = list(AUDIT_ACTION_RULES)
    else:
        visible_actions = [
            action
            for action, rule in AUDIT_ACTION_RULES.items()
            if role in rule.allowed_roles
        ]
    events = list(
        AuditLog.objects.filter(action__in=visible_actions)
        .select_related("actor")
        .order_by("-created_at", "-id")[:limit]
    )
    return [RecentActivity(event=event, url=_activity_url(event, role)) for event in events]


def _parse_date(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AuditQueryError(f"{label}无效") from exc


def _bounded_query_value(params, name, default, max_length):
    value = params.get(name, default)
    if not isinstance(value, str) or len(value) > max_length:
        raise AuditQueryError(f"{name}无效")
    return value.strip()


def audit_query(params):
    today = timezone.localdate()
    start = _parse_date(
        _bounded_query_value(
            params, "start", str(today - timedelta(days=29)), 10
        ),
        "开始日期",
    )
    end = _parse_date(
        _bounded_query_value(params, "end", str(today), 10), "结束日期"
    )
    if end < start or (end - start).days > 92:
        raise AuditQueryError("审计查询最多覆盖 93 天")
    action = _bounded_query_value(params, "action", "", 48)
    if action and action not in AUDIT_ACTION_RULES:
        raise AuditQueryError("审计动作无效")
    target_type = _bounded_query_value(params, "target_type", "", 80)
    allowed_target_types = {rule.target_type for rule in AUDIT_ACTION_RULES.values()}
    if target_type and target_type not in allowed_target_types:
        raise AuditQueryError("审计对象类型无效")
    target_id = _bounded_query_value(params, "target_id", "", 64)
    if target_id and (len(target_id) > 64 or re.fullmatch(r"[A-Za-z0-9-]+", target_id) is None):
        raise AuditQueryError("审计对象标识无效")
    operator = _bounded_query_value(params, "operator", "", 36)
    if operator:
        try:
            operator = str(UUID(operator))
        except ValueError as exc:
            raise AuditQueryError("操作者无效") from exc

    start_at = timezone.make_aware(datetime.combine(start, time.min))
    end_at = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min))
    queryset = AuditLog.objects.filter(
        created_at__gte=start_at, created_at__lt=end_at
    ).select_related("actor")
    if action:
        queryset = queryset.filter(action=action)
    if target_type:
        queryset = queryset.filter(target_type=target_type)
    if target_id:
        queryset = queryset.filter(target_id=target_id)
    if operator:
        queryset = queryset.filter(actor__public_id=operator)
    return queryset.order_by("-created_at", "-id"), {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "operator": operator,
    }
