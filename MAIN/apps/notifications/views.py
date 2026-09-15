from django.contrib.auth import login
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.evaluations.models import EvaluationProject
from apps.evaluations.views.templates import hr_required

from .models import NotificationAttempt, NotificationOutbox
from .services import (
    DELIVERY_IN_PROGRESS,
    InvalidMagicLink,
    NotificationError,
    consume_magic_link_and_bind_user,
    launch_project,
    notification_attempt_preview,
    resend_project_notification,
)


@hr_required
@require_POST
def project_launch(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    try:
        launch_project(project, request.user)
    except NotificationError as exc:
        return render(
            request,
            "hr/notifications/project_status.html",
            _status_context(project, error=str(exc)),
            status=409,
        )
    return redirect("notifications:project-status", project_id=project.public_id)


def _status_context(project, error=None):
    outboxes = list(
        NotificationOutbox.objects.filter(project=project)
        .select_related("recipient")
        .prefetch_related("attempts")
        .order_by("recipient__employee_no")
    )
    for outbox in outboxes:
        outbox.attempt_history = sorted(
            outbox.attempts.all(), key=lambda row: row.attempt
        )
        for attempt in outbox.attempt_history:
            attempt.delivery_preview = notification_attempt_preview(attempt)
        outbox.latest_attempt = (
            outbox.attempt_history[-1] if outbox.attempt_history else None
        )
        outbox.requires_manual_reconciliation = bool(
            outbox.latest_attempt
            and outbox.latest_attempt.status == NotificationAttempt.Status.QUEUED
            and outbox.latest_attempt.failure_code == DELIVERY_IN_PROGRESS
        )
        outbox.reconciliation_reason_id = (
            f"delivery-reconciliation-{outbox.public_id}"
        )
        outbox.can_mutate = bool(
            project.status == EvaluationProject.Status.ACTIVE
            and project.deadline > timezone.now()
            and outbox.status != NotificationOutbox.Status.SENT
            and not outbox.requires_manual_reconciliation
        )
    return {"project": project, "outboxes": outboxes, "error": error}


@hr_required
@require_GET
def project_status(request, project_id):
    project = get_object_or_404(EvaluationProject, public_id=project_id)
    return render(
        request, "hr/notifications/project_status.html", _status_context(project)
    )


def _resend(request, outbox_id, channel):
    outbox = get_object_or_404(
        NotificationOutbox.objects.select_related("project", "recipient"),
        public_id=outbox_id,
    )
    expected_attempt = request.POST.get("expected_attempt", "")
    if not expected_attempt:
        return render(
            request,
            "hr/notifications/project_status.html",
            _status_context(outbox.project, error="通知状态校验缺失，请刷新后重试"),
            status=409,
        )
    try:
        resend_project_notification(
            outbox.project,
            outbox.recipient,
            channel,
            request.user,
            expected_attempt=expected_attempt,
        )
    except NotificationError as exc:
        return render(
            request,
            "hr/notifications/project_status.html",
            _status_context(outbox.project, error=str(exc)),
            status=409,
        )
    return redirect(
        "notifications:project-status", project_id=outbox.project.public_id
    )


@hr_required
@require_POST
def retry_notification(request, outbox_id):
    outbox = get_object_or_404(NotificationOutbox, public_id=outbox_id)
    latest = outbox.attempts.order_by("-attempt").first()
    channel = latest.channel if latest else "wecom"
    return _resend(request, outbox_id, channel)


@hr_required
@require_POST
def switch_to_email(request, outbox_id):
    return _resend(request, outbox_id, "email")


def _private_response(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@sensitive_post_parameters("token")
@require_http_methods(["GET", "POST"])
def email_magic_link(request):
    try:
        if request.method == "GET":
            response = render(request, "notifications/email_confirm.html")
        else:
            user = consume_magic_link_and_bind_user(request.POST.get("token", ""))
            login(request, user)
            response = redirect("evaluations:task_list")
    except InvalidMagicLink:
        response = render(
            request,
            "notifications/email_confirm.html",
            {"invalid": True},
            status=403,
        )
    return _private_response(response)


@require_GET
def project_task_entry(request, project_id):
    get_object_or_404(EvaluationProject, public_id=project_id)
    return redirect("evaluations:task_list")
