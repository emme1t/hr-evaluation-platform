from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from uuid import UUID, uuid4

from apps.audit.models import AuditLog
from apps.audit.services import record_audit
from ..forms.projects import DeadlineExtensionForm, ProjectForm
from ..models.projects import EvaluationProject
from apps.notifications.models import NotificationAttempt, NotificationOutbox
from ..services.projects import (
    ProjectError,
    close_project_early,
    extend_project_deadline,
    prepare_project,
    preview_project_tasks,
)
from .templates import hr_required


@hr_required
@require_GET
def project_list(request):
    status = request.GET.get("status", "")
    focus = request.GET.get("focus", "")
    if status not in {"", *EvaluationProject.Status.values} or focus not in {
        "",
        "completion",
        "pending",
        "notifications",
        "anomalies",
    }:
        return render(
            request,
            "hr/projects/list.html",
            {"projects": [], "query_error": True},
            status=400,
        )
    projects = EvaluationProject.objects.order_by("-created_at", "-id")
    if status:
        projects = projects.filter(status=status)
    if focus == "pending":
        projects = projects.filter(tasks__status="pending")
    elif focus == "notifications":
        latest_status = NotificationAttempt.objects.filter(
            outbox_id=OuterRef("pk")
        ).order_by("-attempt", "-id").values("status")[:1]
        failed_project_ids = (
            NotificationOutbox.objects.annotate(
                latest_status=Subquery(latest_status)
            )
            .filter(latest_status=NotificationAttempt.Status.FAILED)
            .values("project_id")
        )
        projects = projects.filter(pk__in=failed_project_ids)
    elif focus == "anomalies":
        projects = projects.filter(
            Q(deadline__lte=timezone.now(), tasks__status="pending")
            | Q(aggregate_results__status="incomplete")
        )
    return render(
        request,
        "hr/projects/list.html",
        {"projects": projects.distinct()[:100], "status": status, "focus": focus},
    )


@hr_required
@require_http_methods(["GET", "POST"])
def project_create(request):
    form = ProjectForm(request.POST or None)
    mutation_key = request.POST.get("mutation_key", "") if request.method == "POST" else str(uuid4())
    audit_key = None
    mutation_valid = request.method != "POST"
    if request.method == "POST":
        try:
            if not mutation_key or str(UUID(mutation_key)) != mutation_key:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            form.add_error(None, "页面操作标识无效，请刷新后重试")
        else:
            mutation_valid = True
            audit_key = f"project-create:{request.user.public_id}:{mutation_key}"
            existing = AuditLog.objects.filter(
                actor=request.user,
                action="PROJECT_CREATED",
                idempotency_key=audit_key,
            ).first()
            if existing is not None:
                project = get_object_or_404(
                    EvaluationProject, public_id=existing.target_id
                )
                return redirect("evaluations:project-preview", project.public_id)
    if request.method == "POST" and mutation_valid and form.is_valid():
        with transaction.atomic():
            project = form.save()
            record_audit(
                request.user,
                "PROJECT_CREATED",
                project,
                {"subject_count": project.subjects.count()},
                idempotency_key=audit_key,
            )
        return redirect("evaluations:project-preview", project.public_id)
    return render(
        request,
        "hr/projects/create.html",
        {"form": form, "mutation_key": mutation_key or str(uuid4())},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
@require_GET
def project_preview(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    preview = preview_project_tasks(project)
    return render(
        request,
        "hr/projects/preview.html",
        {"project": project, "preview": preview},
    )


@hr_required
@require_POST
def project_prepare(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        prepare_project(project, request.user)
    except ProjectError as exc:
        preview = preview_project_tasks(project)
        return render(
            request,
            "hr/projects/preview.html",
            {"project": project, "preview": preview, "error": str(exc)},
            status=400,
        )
    return redirect("evaluations:project-detail", project.public_id)


@hr_required
@require_GET
def project_detail(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    return _render_detail(request, project)


@hr_required
@require_POST
def project_extend_deadline(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    form = DeadlineExtensionForm(request.POST)
    if form.is_valid():
        try:
            extend_project_deadline(project, form.cleaned_data["deadline"], request.user)
        except ProjectError as exc:
            form.add_error("deadline", str(exc))
        else:
            return redirect("evaluations:project-detail", project.public_id)
    return _render_detail(request, project, deadline_form=form, status=400)


@hr_required
@require_POST
def project_close(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        close_project_early(project, request.user)
    except ProjectError as exc:
        return _render_detail(request, project, error=str(exc), status=400)
    return redirect("evaluations:project-detail", project.public_id)


def _render_detail(request, project, deadline_form=None, error=None, status=200):
    return render(
        request,
        "hr/projects/detail.html",
        {
            "project": project,
            "deadline_form": deadline_form or DeadlineExtensionForm(),
            "error": error,
        },
        status=status,
    )
