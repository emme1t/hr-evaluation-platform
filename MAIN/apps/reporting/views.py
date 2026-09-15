import hashlib

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import require_hr_role
from apps.evaluations.models import EvaluationProject

from .forms import SummaryTemplateUploadForm
from .models import SummaryWorkbookTemplate
from .services.issues import export_issue_workbook
from .services.raw import RawExportError, export_raw_zip
from .services.summary import (
    MAX_TEMPLATE_BYTES,
    SummaryTemplateError,
    export_summary_workbook,
    register_summary_template,
)


class AuditIntegrationUnavailable(RuntimeError):
    pass


def _read_uploaded_template(uploaded):
    size = getattr(uploaded, "size", None)
    if not isinstance(size, int) or size < 0 or size > MAX_TEMPLATE_BYTES:
        raise SummaryTemplateError("汇总模板不能超过 10MB")
    chunks = []
    total = 0
    try:
        iterator = uploaded.chunks(chunk_size=64 * 1024)
        for chunk in iterator:
            remaining = MAX_TEMPLATE_BYTES + 1 - total
            bounded = bytes(chunk[:remaining])
            chunks.append(bounded)
            total += len(bounded)
            if total > MAX_TEMPLATE_BYTES:
                raise SummaryTemplateError("汇总模板不能超过 10MB")
    except SummaryTemplateError:
        raise
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        raise SummaryTemplateError("汇总模板读取失败") from exc
    return b"".join(chunks)


def record_export_success(**event):
    """Task 12 load-bearing seam; never stores a second reporting audit history."""
    try:
        from apps.audit.services import record_audit
    except ImportError as exc:
        raise AuditIntegrationUnavailable("中央审计服务尚未接入") from exc
    actions = {
        "summary": "REPORT_SUMMARY_EXPORTED",
        "raw": "REPORT_RAW_EXPORTED",
        "issues": "REPORT_EXCEPTION_EXPORTED",
    }
    try:
        action = actions[event["kind"]]
        record_audit(
            event["actor"],
            action,
            event["project"],
            {
                "artifact_sha256": event["artifact_sha256"],
                "artifact_size": event["artifact_size"],
            },
        )
    except (KeyError, ValueError) as exc:
        raise AuditIntegrationUnavailable("中央审计服务拒绝导出事件") from exc


admin_required = require_hr_role("HR_ADMIN")


def _download_response(payload, filename, content_type):
    response = HttpResponse(payload, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _serve_export(request, project, *, kind, generator, filename, content_type):
    payload = generator()
    response = _download_response(payload, filename, content_type)
    record_export_success(
        actor=request.user,
        project=project,
        kind=kind,
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        artifact_size=len(payload),
    )
    return response


@admin_required
@require_GET
def template_list(request):
    return render(
        request,
        "hr/reporting/template_list.html",
        {"templates": SummaryWorkbookTemplate.objects.all()},
    )


@admin_required
@require_POST
def template_upload(request):
    form = SummaryTemplateUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request, "hr/reporting/template_upload.html", {"form": form}, status=400
        )
    uploaded = form.cleaned_data["file"]
    try:
        payload = _read_uploaded_template(uploaded)
        register_summary_template(payload, uploaded.name, request.user)
    except SummaryTemplateError as exc:
        form.add_error("file", str(exc))
        return render(
            request, "hr/reporting/template_upload.html", {"form": form}, status=400
        )
    return redirect("reporting:template-list")


@require_POST
@admin_required
def summary_download(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        return _serve_export(
            request,
            project,
            kind="summary",
            generator=lambda: export_summary_workbook(project),
            filename=f"summary-{project.public_id}.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except SummaryTemplateError:
        return HttpResponse("汇总导出失败", status=409)
    except AuditIntegrationUnavailable:
        return HttpResponse("中央审计服务不可用", status=503)


@require_POST
@admin_required
def raw_download(request, project_id, subject_id=None):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        return _serve_export(
            request,
            project,
            kind="raw",
            generator=lambda: export_raw_zip(project, subject_id, actor=request.user),
            filename=f"raw-{project.public_id}.zip",
            content_type="application/zip",
        )
    except RawExportError as exc:
        if exc.code == "SUBJECT_NOT_IN_PROJECT":
            raise Http404 from exc
        return HttpResponse("原始数据导出失败", status=409)
    except AuditIntegrationUnavailable:
        return HttpResponse("中央审计服务不可用", status=503)


@require_POST
@admin_required
def issues_download(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        return _serve_export(
            request,
            project,
            kind="issues",
            generator=lambda: export_issue_workbook(project),
            filename=f"issues-{project.public_id}.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except SummaryTemplateError:
        return HttpResponse("异常说明导出失败", status=409)
    except AuditIntegrationUnavailable:
        return HttpResponse("中央审计服务不可用", status=503)
