import hashlib
import re
from collections.abc import Mapping
from itertools import islice

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.services import record_audit

from ..models import EvaluationProject, EvaluationTask, Submission, SubmissionAnswer
from .frozen_snapshots import (
    MAX_TEMPLATE_SNAPSHOT_ITEMS,
    FrozenTemplateSnapshotValidationError,
    normalize_frozen_template_snapshot,
)


MAX_IDEMPOTENCY_KEY_LENGTH = 128
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
EXPECTED_RACE_CONSTRAINTS = {
    "one_final_submission_per_task",
    "unique_task_idempotency_key",
}


class SubmissionError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class SubmissionAccessError(SubmissionError):
    pass


class SubmissionValidationError(SubmissionError):
    pass


class SubmissionStateError(SubmissionError):
    pass


class SubmissionIdempotencyConflict(SubmissionError):
    pass


class StaleDraftError(SubmissionStateError):
    def __init__(self, current_version):
        super().__init__("草稿版本已更新", "DRAFT_VERSION_STALE")
        self.current_version = current_version


def save_draft(task, user, answers, expected_version=None):
    with transaction.atomic():
        locked_task = _lock_task(task)
        _require_owner(locked_task, user)
        draft = (
            Submission.objects.select_for_update(of=("self",))
            .filter(task=locked_task, is_final=False)
            .first()
        )
        _require_expected_draft_version(draft, expected_version)
        snapshot = _editable_snapshot(locked_task)
        normalized_answers = _normalize_answers(answers, snapshot)

        if draft is None:
            draft = Submission.objects.create(task=locked_task)
        else:
            draft.version += 1
            draft.save(update_fields=["version", "saved_at"])
        _replace_answers(draft, normalized_answers)
        return draft


def _require_expected_draft_version(draft, expected_version):
    if expected_version is None:
        return
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise SubmissionValidationError(
            "草稿版本无效", "DRAFT_VERSION_INVALID"
        )
    current_version = draft.version if draft is not None else 0
    if expected_version != current_version:
        raise StaleDraftError(current_version)


def submit_task(task, user, answers, idempotency_key):
    try:
        return _submit_task_atomic(task, user, answers, idempotency_key)
    except IntegrityError as exc:
        if not _is_expected_submission_race(exc):
            raise
        return _resolve_submission_race(task, user, answers, idempotency_key)


@transaction.atomic
def _submit_task_atomic(task, user, answers, idempotency_key):
    locked_task = _lock_task(task)
    _require_owner(locked_task, user)
    existing = _existing_final(locked_task)
    if existing is not None:
        return _resolve_existing_final_request(
            existing, idempotency_key, answers
        )
    key = _normalize_idempotency_key(idempotency_key)
    snapshot = _validated_snapshot(locked_task)
    normalized_answers = _normalize_answers(answers, snapshot, require_complete=True)

    if locked_task.status == EvaluationTask.Status.SUBMITTED:
        raise SubmissionStateError("该任务已提交", "TASK_ALREADY_SUBMITTED")
    _require_editable_state(locked_task)

    submission = Submission.objects.filter(
        task=locked_task, is_final=False
    ).first()
    now = timezone.now()
    if submission is None:
        submission = Submission.objects.create(
            task=locked_task,
            is_final=True,
            idempotency_key=key,
            submitted_at=now,
        )
    else:
        submission.is_final = True
        submission.idempotency_key = key
        submission.submitted_at = now
        submission.version += 1
        submission.save(
            update_fields=[
                "is_final",
                "idempotency_key",
                "submitted_at",
                "version",
                "saved_at",
            ]
        )
    _replace_answers(submission, normalized_answers)
    locked_task.status = EvaluationTask.Status.SUBMITTED
    locked_task.save(update_fields=["status"])
    audit_key = hashlib.sha256(
        f"{locked_task.public_id}:{key}".encode("utf-8")
    ).hexdigest()
    record_audit(
        user,
        "EVALUATION_SUBMITTED",
        locked_task,
        {"submission": "final"},
        idempotency_key=f"submission:{audit_key}",
        project=locked_task.project,
    )
    task.status = locked_task.status
    return submission


@transaction.atomic
def _resolve_submission_race(task, user, answers, idempotency_key):
    locked_task = _lock_task(task)
    _require_owner(locked_task, user)
    existing = _existing_final(locked_task)
    if existing is None:
        raise SubmissionStateError(
            "提交发生并发冲突，请重试", "SUBMISSION_CONFLICT"
        )
    return _resolve_existing_final_request(
        existing, idempotency_key, answers
    )


def _lock_task(task):
    task_id = getattr(task, "pk", None)
    if task_id is None:
        raise SubmissionAccessError("任务不存在", "TASK_NOT_FOUND")
    try:
        return (
            EvaluationTask.objects.select_for_update(of=("self",))
            .select_related("project", "project_subject", "evaluator")
            .get(pk=task_id)
        )
    except EvaluationTask.DoesNotExist as exc:
        raise SubmissionAccessError("任务不存在", "TASK_NOT_FOUND") from exc


def _require_owner(task, user):
    if (
        not getattr(user, "is_authenticated", False)
        or task.evaluator.user_id != user.pk
        or not task.evaluator.is_active
    ):
        raise SubmissionAccessError("任务不存在", "TASK_NOT_FOUND")


def _validated_snapshot(task):
    try:
        return normalize_frozen_template_snapshot(
            task.project_subject.template_snapshot
        )
    except FrozenTemplateSnapshotValidationError as exc:
        raise SubmissionValidationError(
            "评价任务数据异常，请联系管理员",
            "FROZEN_TEMPLATE_SNAPSHOT_INVALID",
        ) from exc


def _editable_snapshot(task):
    snapshot = _validated_snapshot(task)
    _require_editable_state(task)
    return snapshot


def _require_editable_state(task):
    if task.project.status != EvaluationProject.Status.ACTIVE:
        raise SubmissionStateError("该任务当前不可提交", "PROJECT_NOT_ACTIVE")
    if task.project.deadline <= timezone.now():
        raise SubmissionStateError("该任务已截止", "TASK_DEADLINE_PASSED")
    if task.status != EvaluationTask.Status.PENDING:
        raise SubmissionStateError("该任务不可编辑", "TASK_NOT_EDITABLE")


def _bounded_answer_pairs(answers):
    try:
        source = answers.items() if isinstance(answers, Mapping) else iter(answers)
        pairs = list(islice(source, MAX_TEMPLATE_SNAPSHOT_ITEMS + 1))
    except (TypeError, ValueError) as exc:
        raise SubmissionValidationError(
            "评价内容格式无效", "ANSWERS_MALFORMED"
        ) from exc
    if len(pairs) > MAX_TEMPLATE_SNAPSHOT_ITEMS:
        raise SubmissionValidationError(
            "评价项数量过多", "ANSWERS_TOO_LARGE"
        )
    return pairs


def _normalize_answers(answers, snapshot, require_complete=False):
    pairs = _bounded_answer_pairs(answers)
    items_by_id = {item.snapshot_item_id: item for item in snapshot.items}
    normalized = {}
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise SubmissionValidationError(
                "评价内容格式无效", "ANSWERS_MALFORMED"
            )
        item_id, score = pair
        if not isinstance(item_id, str):
            raise SubmissionValidationError("评价项不存在", "ANSWER_UNKNOWN")
        if item_id in normalized:
            raise SubmissionValidationError("评价项不能重复", "ANSWER_DUPLICATE")
        item = items_by_id.get(item_id)
        if item is None:
            raise SubmissionValidationError("评价项不存在", "ANSWER_UNKNOWN")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise SubmissionValidationError(
                "评分必须为 1 到 5 的整数", "SCORE_INVALID"
            )
        if not item.score_min <= score <= item.score_max:
            raise SubmissionValidationError(
                "评分超出该评价项范围", "SCORE_OUT_OF_RANGE"
            )
        normalized[item_id] = score
    if require_complete and set(normalized) != set(items_by_id):
        raise SubmissionValidationError(
            "请完成全部必填评价项", "ANSWERS_INCOMPLETE"
        )
    return normalized


def _normalize_replay_answers(answers):
    normalized = {}
    for pair in _bounded_answer_pairs(answers):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise SubmissionValidationError(
                "评价内容格式无效", "ANSWERS_MALFORMED"
            )
        item_id, score = pair
        if not isinstance(item_id, str):
            raise SubmissionValidationError("评价项不存在", "ANSWER_UNKNOWN")
        try:
            if item_id in normalized:
                raise SubmissionValidationError(
                    "评价项不能重复", "ANSWER_DUPLICATE"
                )
            normalized[item_id] = score
        except TypeError as exc:
            raise SubmissionValidationError(
                "评价内容格式无效", "ANSWERS_MALFORMED"
            ) from exc
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise SubmissionValidationError(
                "评分必须为 1 到 5 的整数", "SCORE_INVALID"
            )
    return normalized


def _replace_answers(submission, answers):
    submission.answers.all().delete()
    SubmissionAnswer.objects.bulk_create(
        [
            SubmissionAnswer(
                submission=submission,
                item_snapshot_id=item_id,
                score=score,
            )
            for item_id, score in answers.items()
        ]
    )


def _normalize_idempotency_key(key):
    if (
        not isinstance(key, str)
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or IDEMPOTENCY_KEY_PATTERN.fullmatch(key) is None
    ):
        raise SubmissionIdempotencyConflict(
            "提交标识无效", "IDEMPOTENCY_KEY_INVALID"
        )
    return key


def _existing_final(task):
    return Submission.objects.filter(task=task, is_final=True).first()


def _resolve_existing_final(existing, key, answers):
    if existing.idempotency_key != key:
        raise SubmissionStateError("该任务已提交", "TASK_ALREADY_SUBMITTED")
    stored_answers = {
        str(item_id): score
        for item_id, score in existing.answers.values_list(
            "item_snapshot_id", "score"
        )
    }
    if stored_answers != answers:
        raise SubmissionIdempotencyConflict(
            "幂等键已用于不同的提交内容",
            "IDEMPOTENCY_PAYLOAD_CONFLICT",
        )
    return existing


def _resolve_existing_final_request(existing, key, answers):
    if existing.idempotency_key != key:
        raise SubmissionStateError("该任务已提交", "TASK_ALREADY_SUBMITTED")
    try:
        normalized_answers = _normalize_replay_answers(answers)
    except SubmissionValidationError as exc:
        raise SubmissionIdempotencyConflict(
            "幂等键已用于不同的提交内容",
            "IDEMPOTENCY_PAYLOAD_CONFLICT",
        ) from exc
    return _resolve_existing_final(existing, key, normalized_answers)


def _constraint_name(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diag = getattr(current, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if name:
            return name
        current = getattr(current, "__cause__", None)
    return None


def _is_expected_submission_race(error):
    if _constraint_name(error) in EXPECTED_RACE_CONSTRAINTS:
        return True
    message = str(error).lower()
    return connection.vendor == "sqlite" and (
        "unique constraint failed: evaluations_submission.task_id" in message
        or (
            "unique constraint failed: evaluations_submission.task_id, "
            "evaluations_submission.idempotency_key"
        )
        in message
    )
