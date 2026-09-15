from hashlib import sha256
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import AuditContractError, record_audit
from apps.evaluations.models import (
    AggregateResult,
    EvaluationProject,
    EvaluationTask,
)
from apps.evaluations.services.scoring import (
    ALGORITHM_VERSION,
    ScoringError,
    calculate_project_results,
    load_project_scoring_source,
)


RECOMPUTABLE_PROJECT_STATUSES = frozenset(
    {
        EvaluationProject.Status.READY,
        EvaluationProject.Status.ACTIVE,
        EvaluationProject.Status.CLOSED,
    }
)


class Command(BaseCommand):
    help = "Recompute auditable aggregate results from frozen project inputs."

    def add_arguments(self, parser):
        parser.add_argument("--project", required=True)
        parser.add_argument("--actor")

    @transaction.atomic
    def handle(self, *args, **options):
        project_public_id = _parse_project_id(options["project"])
        actor = _resolve_actor(options.get("actor"))
        project = _lock_project(project_public_id)
        if (
            project.status not in RECOMPUTABLE_PROJECT_STATUSES
            or project.prepared_at is None
        ):
            raise CommandError("PROJECT_NOT_READY")
        locked_tasks = _lock_project_tasks(project)
        existing_results = list(
            AggregateResult.objects.select_for_update()
            .filter(project=project, algorithm_version=ALGORITHM_VERSION)
            .order_by("project_subject_id", "pk")
        )
        try:
            source = load_project_scoring_source(project, tasks=locked_tasks)
            outcomes = calculate_project_results(source)
        except ScoringError as exc:
            raise CommandError(f"SCORING_FAILED:{exc.code}") from exc
        if not outcomes:
            raise CommandError("PROJECT_NOT_READY")

        calculated_at = timezone.now()
        current_project_subject_ids = [item.pk for item, _ in outcomes]
        AggregateResult.objects.filter(
            project=project,
            algorithm_version=ALGORITHM_VERSION,
        ).exclude(project_subject_id__in=current_project_subject_ids).delete()

        existing_by_project_subject = {
            result.project_subject_id: result for result in existing_results
        }
        to_update = []
        to_create = []
        for project_subject, outcome in outcomes:
            values = _result_values(project_subject, outcome, calculated_at)
            result = existing_by_project_subject.get(project_subject.pk)
            if result is None:
                to_create.append(
                    AggregateResult(
                        project=project,
                        project_subject=project_subject,
                        algorithm_version=ALGORITHM_VERSION,
                        **values,
                    )
                )
                continue
            for field, value in values.items():
                setattr(result, field, value)
            to_update.append(result)
        if to_update:
            AggregateResult.objects.bulk_update(
                to_update,
                fields=list(
                    _result_values(
                        outcomes[0][0], outcomes[0][1], calculated_at
                    )
                ),
            )
        if to_create:
            AggregateResult.objects.bulk_create(to_create)

        if actor is not None:
            fingerprint = sha256(
                "|".join(
                    sorted(outcome.input_fingerprint for _subject, outcome in outcomes)
                ).encode("ascii")
            ).hexdigest()
            try:
                record_audit(
                    actor,
                    "PROJECT_RECOMPUTED",
                    project,
                    {"result_count": len(outcomes), "algorithm": ALGORITHM_VERSION},
                    idempotency_key=(
                        f"project:{project.public_id}:recomputed:{fingerprint}"
                    ),
                )
            except AuditContractError as exc:
                raise CommandError("ACTOR_NOT_AUTHORIZED") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"project={project.public_id} recomputed={len(outcomes)} "
                f"algorithm={ALGORITHM_VERSION}"
            )
        )


def _lock_project(project_public_id):
    try:
        return EvaluationProject.objects.select_for_update().get(
            public_id=project_public_id
        )
    except EvaluationProject.DoesNotExist as exc:
        raise CommandError("PROJECT_NOT_FOUND") from exc


def _lock_project_tasks(project):
    return list(
        EvaluationTask.objects.select_for_update(of=("self",))
        .filter(project=project)
        .select_related("evaluator")
        .order_by("pk")
    )


def _result_values(project_subject, outcome, calculated_at):
    return {
        "subject_id": project_subject.subject_id,
        "frozen_subject_public_id": outcome.subject_public_id,
        "frozen_subject_name": outcome.subject_name,
        "manager_score": outcome.group_scores["manager"],
        "same_department_score": outcome.group_scores["same_department"],
        "cross_department_score": outcome.group_scores["cross_department"],
        "total_score": outcome.total_score,
        "status": outcome.status,
        "missing_groups": list(outcome.missing_groups),
        "valid_submission_count": outcome.valid_submission_count,
        "calculated_at": calculated_at,
        "integrity_status": AggregateResult.IntegrityStatus.VERIFIED,
        "input_fingerprint": outcome.input_fingerprint,
    }


def _parse_project_id(value):
    if not isinstance(value, str):
        raise CommandError("PROJECT_ID_INVALID")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise CommandError("PROJECT_ID_INVALID") from exc
    if str(parsed) != value:
        raise CommandError("PROJECT_ID_INVALID")
    return parsed


def _resolve_actor(value):
    if not value:
        if settings.APP_ENV != "test":
            raise CommandError("ACTOR_REQUIRED")
        return None
    actor_id = _parse_project_id(value)
    try:
        return get_user_model().objects.get(public_id=actor_id, is_active=True)
    except get_user_model().DoesNotExist as exc:
        raise CommandError("ACTOR_INVALID") from exc
