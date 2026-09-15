from dataclasses import dataclass
from functools import wraps
import logging
from uuid import UUID, uuid4

from django.contrib.auth.views import redirect_to_login
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.roster.models import Employee

from ..forms.submissions import (
    SubmissionPayloadError,
    parse_draft_request,
    parse_submit_request,
)
from ..models import EvaluationTask, Submission
from ..querysets import tasks_for_user
from ..services.frozen_snapshots import (
    FrozenTemplateSnapshotValidationError,
    normalize_frozen_template_snapshot,
)
from ..services.submissions import (
    StaleDraftError,
    SubmissionAccessError,
    SubmissionError,
    SubmissionStateError,
    save_draft,
    submit_task,
)


logger = logging.getLogger(__name__)

SUBJECT_SNAPSHOT_KEYS = {
    "public_id",
    "name",
    "department_level_1",
    "department_level_2",
}


class SnapshotPresentationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TaskPresentation:
    subject_name: str
    evaluation_items: tuple[dict, ...]


@dataclass
class ProjectTaskGroup:
    project: object
    buckets: dict[str, list]


def evaluator_login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(
                request.get_full_path(), login_url=reverse("accounts:wecom_start")
            )
        return view(request, *args, **kwargs)

    return wrapped


def _task_bucket(task, now):
    if task.status == EvaluationTask.Status.SUBMITTED:
        return "submitted"
    if (
        task.status == EvaluationTask.Status.NOT_SUBMITTED
        or task.project.status == task.project.Status.CLOSED
        or task.project.deadline <= now
    ):
        return "expired"
    if getattr(task, "has_draft", False):
        return "draft"
    return "pending"


def _project_task_groups(tasks, now):
    project_groups = {}
    for task in tasks:
        group = project_groups.setdefault(
            task.project_id,
            ProjectTaskGroup(
                project=task.project,
                buckets={
                    "pending": [],
                    "draft": [],
                    "submitted": [],
                    "expired": [],
                },
            ),
        )
        group.buckets[_task_bucket(task, now)].append(task)
    return list(project_groups.values())


def _is_read_only(task, now):
    return (
        task.status != EvaluationTask.Status.PENDING
        or task.project.status != task.project.Status.ACTIVE
        or task.project.deadline <= now
    )


def _presentation_error(code):
    raise SnapshotPresentationError(code)


def _is_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _is_nonempty_string(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _task_presentation(project_subject, selected_scores=None):
    selected_scores = selected_scores or {}
    subject_snapshot = project_subject.subject_snapshot
    if (
        not isinstance(subject_snapshot, dict)
        or set(subject_snapshot) != SUBJECT_SNAPSHOT_KEYS
        or not _is_uuid(subject_snapshot.get("public_id"))
        or not _is_nonempty_string(subject_snapshot.get("name"), 100)
        or not _is_nonempty_string(subject_snapshot.get("department_level_1"), 120)
        or not _is_nonempty_string(subject_snapshot.get("department_level_2"), 120)
    ):
        _presentation_error("TASK_SUBJECT_SNAPSHOT_INVALID")

    snapshot = normalize_frozen_template_snapshot(
        project_subject.template_snapshot
    )
    evaluation_items = tuple(
        {
            "snapshot_item_id": item.snapshot_item_id,
            "group": item.group,
            "title": item.title,
            "order": item.order,
            "score_values": range(item.score_min, item.score_max + 1),
            "selected_score": selected_scores.get(item.snapshot_item_id),
        }
        for item in snapshot.items
    )
    return TaskPresentation(
        subject_name=subject_snapshot["name"],
        evaluation_items=tuple(evaluation_items),
    )


@evaluator_login_required
@require_GET
def task_list(request):
    if not Employee.objects.filter(user=request.user, is_active=True).exists():
        return render(request, "evaluator/forbidden.html", status=403)
    now = timezone.now()
    return render(
        request,
        "evaluator/task_list.html",
        {"project_groups": _project_task_groups(tasks_for_user(request.user), now)},
    )


@evaluator_login_required
@require_GET
def task_detail(request, public_id):
    task = get_object_or_404(tasks_for_user(request.user), public_id=public_id)
    submission = _displayed_submission(task)
    selected_scores = _submission_scores(submission)
    return _render_task_form(
        request,
        task,
        selected_scores=selected_scores,
        draft_version=(
            submission.version
            if submission is not None and not submission.is_final
            else 0
        ),
        idempotency_key=str(uuid4()),
    )


@evaluator_login_required
@require_POST
def task_draft(request, public_id):
    task = get_object_or_404(tasks_for_user(request.user), public_id=public_id)
    try:
        answers, expected_version = parse_draft_request(request)
        draft = save_draft(
            task,
            request.user,
            answers,
            expected_version=expected_version,
        )
    except SubmissionPayloadError as exc:
        return _json_error(exc, exc.status)
    except SubmissionAccessError as exc:
        raise Http404 from exc
    except StaleDraftError as exc:
        return JsonResponse(
            {
                "error": {"code": exc.code, "message": str(exc)},
                "current_version": exc.current_version,
            },
            status=409,
        )
    except SubmissionStateError as exc:
        return _json_error(exc, 409)
    except SubmissionError as exc:
        return _json_error(exc, _submission_error_status(exc))
    return JsonResponse(
        {"saved_at": draft.saved_at.isoformat(), "version": draft.version}
    )


@evaluator_login_required
@require_POST
def task_submit(request, public_id):
    task = get_object_or_404(tasks_for_user(request.user), public_id=public_id)
    try:
        answers, idempotency_key = parse_submit_request(request)
        submit_task(task, request.user, answers, idempotency_key)
    except SubmissionPayloadError as exc:
        return _render_task_form(
            request,
            task,
            selected_scores=_score_pairs_for_display(locals().get("answers", ())),
            idempotency_key=locals().get("idempotency_key") or "",
            submission_error=str(exc),
            status=exc.status,
        )
    except SubmissionAccessError as exc:
        raise Http404 from exc
    except SubmissionError as exc:
        status = _submission_error_status(exc)
        if exc.code == "FROZEN_TEMPLATE_SNAPSHOT_INVALID":
            return _snapshot_error_response(request, task, exc)
        return _render_task_form(
            request,
            task,
            selected_scores=_score_pairs_for_display(answers),
            idempotency_key=idempotency_key or "",
            submission_error=str(exc),
            status=status,
        )
    return redirect("evaluations:task_detail", public_id=task.public_id)


def _displayed_submission(task):
    submissions = list(
        Submission.objects.filter(task=task)
        .prefetch_related("answers")
        .order_by("-is_final", "-id")[:2]
    )
    return submissions[0] if submissions else None


def _submission_scores(submission):
    if submission is None:
        return {}
    return {
        str(answer.item_snapshot_id): answer.score
        for answer in submission.answers.all()
    }


def _score_pairs_for_display(pairs):
    scores = {}
    for pair in pairs:
        if isinstance(pair, (tuple, list)) and len(pair) == 2:
            item_id, score = pair
            if isinstance(item_id, str) and isinstance(score, int):
                scores[item_id] = score
    return scores


def _render_task_form(
    request,
    task,
    *,
    selected_scores=None,
    draft_version=None,
    idempotency_key="",
    submission_error=None,
    status=200,
):
    now = timezone.now()
    try:
        presentation = _task_presentation(task.project_subject, selected_scores)
    except (SnapshotPresentationError, FrozenTemplateSnapshotValidationError) as exc:
        return _snapshot_error_response(request, task, exc)
    is_submitted = task.status == EvaluationTask.Status.SUBMITTED
    if draft_version is None:
        draft_version = (
            Submission.objects.filter(task=task, is_final=False)
            .values_list("version", flat=True)
            .first()
            or 0
        )
    return render(
        request,
        "evaluator/task_form.html",
        {
            "task": task,
            "subject_name": presentation.subject_name,
            "evaluation_items": presentation.evaluation_items,
            "is_read_only": _is_read_only(task, now),
            "is_submitted": is_submitted,
            "draft_version": draft_version,
            "idempotency_key": idempotency_key,
            "submission_error": submission_error,
        },
        status=status,
    )


def _snapshot_error_response(request, task, exc):
    logger.warning(
        "evaluator_task_snapshot_invalid error_code=%s task_public_id=%s project_public_id=%s",
        exc.code,
        task.public_id,
        task.project.public_id,
    )
    return render(request, "evaluator/snapshot_error.html", status=409)


def _json_error(exc, status):
    return JsonResponse(
        {"error": {"code": exc.code, "message": str(exc)}}, status=status
    )


def _submission_error_status(exc):
    if exc.code in {"ANSWERS_TOO_LARGE", "PAYLOAD_TOO_LARGE"}:
        return 413
    if (
        isinstance(exc, SubmissionStateError)
        or exc.code == "IDEMPOTENCY_PAYLOAD_CONFLICT"
    ):
        return 409
    return 400
