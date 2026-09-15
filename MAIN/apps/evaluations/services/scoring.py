import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from uuid import UUID

from apps.roster.models import EvaluationRelationship

from ..models import EvaluationProject, EvaluationTask, ProjectSubject, Submission
from .frozen_snapshots import (
    FrozenTemplateSnapshotValidationError,
    normalize_frozen_template_snapshot,
)
from .projects import ProjectStateError, RULE_GROUPS, validate_ready_project_integrity


SCORE_QUANTUM = Decimal("0.01")
ALGORITHM_VERSION = "50-30-20-v1"


class ScoringError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class ScoringIntegrityError(ScoringError):
    pass


@dataclass(frozen=True)
class AggregateOutcome:
    subject_public_id: UUID
    subject_name: str
    group_scores: MappingProxyType
    total_score: Decimal | None
    status: str
    missing_groups: tuple[str, ...]
    valid_submission_count: int
    input_fingerprint: str


@dataclass(frozen=True)
class FinalSubmissionInput:
    submission: Submission
    answers: tuple


@dataclass(frozen=True)
class ProjectScoringSource:
    project: EvaluationProject
    project_subjects: tuple[ProjectSubject, ...]
    tasks_by_project_subject: MappingProxyType
    submissions_by_task: MappingProxyType
    relationship_weights: MappingProxyType
    subject_identities: MappingProxyType


def calculate_submission_score(submission):
    """Return one final submission score rounded only at the public boundary."""
    return _calculate_submission_score(_load_submission(submission)).quantize(
        SCORE_QUANTUM, rounding=ROUND_HALF_UP
    )


def calculate_subject_result(project, subject):
    """Calculate one subject solely from frozen project and final submission rows."""
    project_id = getattr(project, "pk", None)
    subject_id = getattr(subject, "pk", None)
    if project_id is None or subject_id is None:
        raise ScoringIntegrityError(
            "项目或被评价人不存在", "PROJECT_SUBJECT_NOT_FOUND"
        )
    try:
        frozen_project = EvaluationProject.objects.get(pk=project_id)
    except EvaluationProject.DoesNotExist as exc:
        raise ScoringIntegrityError(
            "项目或被评价人不存在", "PROJECT_SUBJECT_NOT_FOUND"
        ) from exc
    source = load_project_scoring_source(frozen_project)
    for project_subject, outcome in calculate_project_results(source):
        if project_subject.subject_id == subject_id:
            return outcome
    raise ScoringIntegrityError(
        "项目或被评价人不存在", "PROJECT_SUBJECT_NOT_FOUND"
    )


def load_project_scoring_source(project, *, tasks=None):
    """Bulk-load one project source; callers may supply already-locked tasks."""
    project_subjects = list(
        ProjectSubject.objects.filter(project_id=project.pk)
        .select_related("subject")
        .order_by("public_id")
    )
    if tasks is None:
        tasks = list(
            EvaluationTask.objects.filter(project_id=project.pk)
            .select_related("evaluator")
            .order_by("public_id")
        )
    else:
        tasks = list(tasks)
    final_submissions = list(
        Submission.objects.filter(
            task_id__in=[task.pk for task in tasks], is_final=True
        )
        .prefetch_related("answers")
        .order_by("public_id")
    )
    return build_project_scoring_source(
        project,
        project_subjects,
        tasks,
        final_submissions,
    )


def build_project_scoring_source(
    project,
    project_subjects,
    tasks,
    final_submissions,
):
    """Build and validate a deterministic in-memory scoring source."""
    project_subjects = tuple(
        sorted(project_subjects, key=lambda item: str(item.public_id))
    )
    tasks = tuple(sorted(tasks, key=lambda item: str(item.public_id)))
    final_submissions = tuple(
        sorted(final_submissions, key=lambda item: str(item.public_id))
    )
    try:
        validate_ready_project_integrity(
            project, project_subjects=project_subjects, tasks=tasks
        )
    except ProjectStateError as exc:
        raise ScoringIntegrityError(
            "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
        ) from exc
    project_subjects_by_id = {item.pk: item for item in project_subjects}
    tasks_by_project_subject = defaultdict(list)
    tasks_by_id = {}
    for task in tasks:
        project_subject = project_subjects_by_id.get(task.project_subject_id)
        if project_subject is None or task.project_id != project.pk:
            raise ScoringIntegrityError(
                "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
            )
        task.project_subject = project_subject
        tasks_by_project_subject[project_subject.pk].append(task)
        tasks_by_id[task.pk] = task
    subject_identities = {}
    for project_subject in project_subjects:
        if project_subject.project_id != project.pk:
            raise ScoringIntegrityError(
                "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
            )
        subject_identities[project_subject.pk] = _frozen_subject_identity(
            project_subject
        )
        _validate_frozen_task_evaluators(
            project_subject,
            tasks_by_project_subject[project_subject.pk],
        )

    submissions_by_task = defaultdict(list)
    for submission in final_submissions:
        if submission.task_id not in tasks_by_id:
            raise ScoringIntegrityError(
                "正式提交与项目任务不一致", "SUBMISSION_TASK_MISMATCH"
            )
        submissions_by_task[submission.task_id].append(
            FinalSubmissionInput(
                submission=submission,
                answers=tuple(submission.answers.all()),
            )
        )
    return ProjectScoringSource(
        project=project,
        project_subjects=project_subjects,
        tasks_by_project_subject=MappingProxyType(
            {
                key: tuple(value)
                for key, value in tasks_by_project_subject.items()
            }
        ),
        submissions_by_task=MappingProxyType(
            {key: tuple(value) for key, value in submissions_by_task.items()}
        ),
        relationship_weights=MappingProxyType(
            _frozen_relationship_weights(project.rule_snapshot)
        ),
        subject_identities=MappingProxyType(subject_identities),
    )


def calculate_project_results(source):
    return tuple(
        (
            project_subject,
            _calculate_subject_result_from_source(source, project_subject),
        )
        for project_subject in source.project_subjects
    )


def _calculate_subject_result_from_source(source, project_subject):
    tasks = source.tasks_by_project_subject.get(project_subject.pk, ())
    subject_public_id, subject_name = source.subject_identities[project_subject.pk]

    raw_scores = {group: [] for group in RULE_GROUPS}
    fingerprint_submissions = []
    for task in tasks:
        task_finals = source.submissions_by_task.get(task.pk, ())
        if len(task_finals) > 1:
            raise ScoringIntegrityError(
                "任务存在重复正式提交", "DUPLICATE_FINAL_SUBMISSION"
            )
        if not task_finals:
            continue
        submission_input = task_finals[0]
        raw_score = _calculate_submission_score(
            submission_input.submission,
            expected_task=task,
            answers=submission_input.answers,
        )
        raw_scores[task.relationship_type].append(raw_score)
        fingerprint_submissions.append(
            _submission_fingerprint_payload(submission_input, task)
        )

    raw_group_scores = {
        group: (
            sum(scores, Decimal("0")) / Decimal(len(scores)) if scores else None
        )
        for group, scores in raw_scores.items()
    }
    missing_groups = tuple(
        group for group in RULE_GROUPS if raw_group_scores[group] is None
    )
    if missing_groups:
        total_score = None
        status = "incomplete"
    else:
        raw_total = sum(
            raw_group_scores[group] * source.relationship_weights[group]
            for group in RULE_GROUPS
        )
        total_score = raw_total.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)
        status = "complete"

    public_group_scores = MappingProxyType(
        {
            group: (
                score.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)
                if score is not None
                else None
            )
            for group, score in raw_group_scores.items()
        }
    )
    fingerprint = _input_fingerprint(
        source.project,
        project_subject,
        tasks,
        fingerprint_submissions,
    )
    return AggregateOutcome(
        subject_public_id=subject_public_id,
        subject_name=subject_name,
        group_scores=public_group_scores,
        total_score=total_score,
        status=status,
        missing_groups=missing_groups,
        valid_submission_count=len(fingerprint_submissions),
        input_fingerprint=fingerprint,
    )


def _load_submission(submission):
    submission_id = getattr(submission, "pk", None)
    if submission_id is None:
        raise ScoringIntegrityError("正式提交不存在", "SUBMISSION_NOT_FOUND")
    try:
        return (
            Submission.objects.select_related(
                "task__project", "task__project_subject"
            )
            .prefetch_related("answers")
            .get(pk=submission_id)
        )
    except Submission.DoesNotExist as exc:
        raise ScoringIntegrityError(
            "正式提交不存在", "SUBMISSION_NOT_FOUND"
        ) from exc


def _calculate_submission_score(submission, expected_task=None, answers=None):
    task = expected_task if expected_task is not None else submission.task
    if not submission.is_final or submission.submitted_at is None:
        raise ScoringIntegrityError("提交不是正式版本", "SUBMISSION_NOT_FINAL")
    if task.status != EvaluationTask.Status.SUBMITTED:
        raise ScoringIntegrityError(
            "正式提交与任务状态不一致", "SUBMISSION_TASK_STATE_INVALID"
        )
    if expected_task is not None and submission.task_id != expected_task.pk:
        raise ScoringIntegrityError(
            "正式提交与任务不一致", "SUBMISSION_TASK_MISMATCH"
        )
    project_subject = task.project_subject
    if (
        task.project_id != project_subject.project_id
        or task.subject_id != project_subject.subject_id
        or task.relationship_type not in EvaluationRelationship.Type.values
    ):
        raise ScoringIntegrityError(
            "任务冻结关联无效", "SUBMISSION_TASK_MISMATCH"
        )
    try:
        snapshot = normalize_frozen_template_snapshot(
            project_subject.template_snapshot
        )
    except FrozenTemplateSnapshotValidationError as exc:
        raise ScoringIntegrityError(
            "模板冻结快照无效", "FROZEN_TEMPLATE_SNAPSHOT_INVALID"
        ) from exc

    items_by_id = {item.snapshot_item_id: item for item in snapshot.items}
    answers = list(submission.answers.all()) if answers is None else list(answers)
    answer_ids = [str(answer.item_snapshot_id) for answer in answers]
    if len(answer_ids) != len(set(answer_ids)) or set(answer_ids) != set(items_by_id):
        raise ScoringIntegrityError(
            "正式提交答案集合无效", "SUBMISSION_ANSWERS_INVALID"
        )

    total = Decimal("0")
    for answer in answers:
        item = items_by_id[str(answer.item_snapshot_id)]
        score = answer.score
        if (
            isinstance(score, bool)
            or not isinstance(score, int)
            or not item.score_min <= score <= item.score_max
        ):
            raise ScoringIntegrityError(
                "正式提交评分无效", "SUBMISSION_SCORE_INVALID"
            )
        total += Decimal(score) * Decimal(item.weight)
    return total


def _frozen_relationship_weights(snapshot):
    try:
        if (
            not isinstance(snapshot, dict)
            or tuple(snapshot.get("required_groups", ())) != tuple(RULE_GROUPS)
        ):
            raise ValueError
        weights = {group: Decimal(snapshot[group]) for group in RULE_GROUPS}
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoringIntegrityError(
            "项目规则冻结快照无效", "PROJECT_FROZEN_DATA_INVALID"
        ) from exc
    return weights


def _validate_frozen_task_evaluators(project_subject, tasks):
    try:
        frozen_evaluators = {
            item["snapshot_item_id"]: item["evaluator"]["public_id"]
            for item in project_subject.relationship_snapshot
        }
    except (KeyError, TypeError) as exc:
        raise ScoringIntegrityError(
            "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
        ) from exc
    for task in tasks:
        if frozen_evaluators.get(str(task.relationship_snapshot_item_id)) != str(
            task.evaluator.public_id
        ):
            raise ScoringIntegrityError(
                "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
            )


def _frozen_subject_identity(project_subject):
    snapshot = project_subject.subject_snapshot
    try:
        public_id = UUID(snapshot["public_id"])
        name = snapshot["name"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ScoringIntegrityError(
            "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
        ) from exc
    if str(public_id) != str(project_subject.subject.public_id) or not isinstance(
        name, str
    ) or not name.strip():
        raise ScoringIntegrityError(
            "项目冻结数据无效", "PROJECT_FROZEN_DATA_INVALID"
        )
    return public_id, name


def _submission_fingerprint_payload(submission_input, task):
    submission = submission_input.submission
    return {
        "public_id": str(submission.public_id),
        "task_public_id": str(task.public_id),
        "is_final": submission.is_final,
        "answers": [
            [str(answer.item_snapshot_id), answer.score]
            for answer in sorted(
                submission_input.answers,
                key=lambda answer: str(answer.item_snapshot_id),
            )
        ],
    }


def _input_fingerprint(project, project_subject, tasks, submissions):
    template_snapshot = {
        **project_subject.template_snapshot,
        "items": sorted(
            project_subject.template_snapshot["items"],
            key=lambda item: item["snapshot_item_id"],
        ),
    }
    relationship_snapshot = sorted(
        project_subject.relationship_snapshot,
        key=lambda item: item["snapshot_item_id"],
    )
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "project": str(project.public_id),
        "rule_snapshot": project.rule_snapshot,
        "project_subject": str(project_subject.public_id),
        "subject_snapshot": project_subject.subject_snapshot,
        "template_snapshot": template_snapshot,
        "relationship_snapshot": relationship_snapshot,
        "tasks": [
            {
                "public_id": str(task.public_id),
                "project_subject_public_id": str(project_subject.public_id),
                "subject_public_id": project_subject.subject_snapshot["public_id"],
                "evaluator_public_id": str(task.evaluator.public_id),
                "relationship_type": task.relationship_type,
                "relationship_snapshot_item_id": str(
                    task.relationship_snapshot_item_id
                ),
                "status": task.status,
            }
            for task in sorted(tasks, key=lambda item: str(item.public_id))
        ],
        "submissions": sorted(
            submissions, key=lambda item: item["public_id"]
        ),
    }
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
