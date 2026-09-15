import csv
from io import StringIO
import json

from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import require_hr_role

from .services import (
    AUDIT_ACTION_RULES,
    AuditQueryError,
    AuditContractError,
    audit_query,
    dashboard_metrics,
    recent_activity_for_role,
    record_audit,
)


@require_hr_role("HR_ADMIN", "HR_OPERATOR")
@require_GET
def dashboard(request):
    try:
        metrics = dashboard_metrics()
        recent_activity = recent_activity_for_role(request.hr_role)
    except DatabaseError:
        return render(
            request,
            "hr/dashboard.html",
            {"dashboard_error": True, "metrics": None, "recent_activity": []},
            status=503,
        )
    return render(
        request,
        "hr/dashboard.html",
        {
            "metrics": metrics,
            "recent_activity": recent_activity,
            "metric_links": {
                "employee_count": reverse("roster:roster-list"),
                "active_projects": reverse("evaluations:project-list")
                + "?status=active",
                "completion": reverse("evaluations:project-list")
                + "?status=active&focus=completion",
                "pending_tasks": reverse("evaluations:project-list")
                + "?status=active&focus=pending",
                "failed_outboxes": reverse("evaluations:project-list")
                + "?status=active&focus=notifications",
                "anomalies": reverse("evaluations:project-list")
                + "?status=active&focus=anomalies",
            },
        },
    )


@require_hr_role("HR_ADMIN")
@require_GET
def audit_list(request):
    actions = tuple(AUDIT_ACTION_RULES)
    target_types = sorted(
        {rule.target_type for rule in AUDIT_ACTION_RULES.values()}
    )
    try:
        queryset, filters = audit_query(request.GET)
        raw_page = request.GET.get("page", "1")
        if (
            not isinstance(raw_page, str)
            or len(raw_page) > 5
            or not raw_page.isascii()
            or not raw_page.isdecimal()
        ):
            raise AuditQueryError("页码无效")
        page = int(raw_page)
        if not 1 <= page <= 10_000:
            raise AuditQueryError("页码无效")
        paginator = Paginator(queryset, 50)
        if page > max(1, paginator.num_pages):
            raise AuditQueryError("页码无效")
        page_obj = paginator.page(page)
    except AuditQueryError:
        return render(
            request,
            "hr/audit/list.html",
            {
                "query_error": True,
                "page_obj": None,
                "filters": {},
                "actions": actions,
                "target_types": target_types,
            },
            status=400,
        )
    return render(
        request,
        "hr/audit/list.html",
        {
            "page_obj": page_obj,
            "filters": filters,
            "actions": actions,
            "target_types": target_types,
        },
    )


def _safe_csv_cell(value):
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@require_hr_role("HR_ADMIN")
@require_POST
def audit_export(request):
    try:
        queryset, filters = audit_query(request.POST)
        events = list(queryset[:10_001])
    except AuditQueryError:
        return HttpResponse("审计查询条件无效", status=400)
    if len(events) > 10_000:
        return HttpResponse("审计导出记录过多，请缩小范围", status=413)

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "created_at",
            "actor",
            "effective_role",
            "action",
            "target_type",
            "target_id",
            "correlation_id",
            "change_summary",
        ]
    )
    for event in events:
        writer.writerow(
            [
                event.created_at.isoformat(),
                _safe_csv_cell(event.actor.username),
                event.effective_role,
                event.action,
                event.target_type,
                event.target_id,
                event.correlation_id,
                json.dumps(
                    event.change_summary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    payload = ("\ufeff" + output.getvalue()).encode("utf-8")
    response = HttpResponse(payload, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(
        True, "audit-log.csv"
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    try:
        record_audit(
            request.user,
            "AUDIT_EXPORTED",
            request.user,
            {"filters": filters, "row_count": len(events)},
        )
    except (AuditContractError, DatabaseError):
        return HttpResponse("中央审计服务不可用", status=503)
    return response
