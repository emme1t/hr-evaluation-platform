import uuid
from itertools import combinations

from django.db import models


RESULT_GROUP_FIELDS = (
    ("manager", "manager_score"),
    ("same_department", "same_department_score"),
    ("cross_department", "cross_department_score"),
)


def _status_and_missing_groups_condition():
    condition = models.Q(
        status="complete",
        total_score__isnull=False,
        manager_score__isnull=False,
        same_department_score__isnull=False,
        cross_department_score__isnull=False,
        missing_groups=[],
    )
    for size in range(1, len(RESULT_GROUP_FIELDS) + 1):
        for missing in combinations(RESULT_GROUP_FIELDS, size):
            missing_names = [name for name, _ in missing]
            missing_fields = {field for _, field in missing}
            field_nullability = {
                f"{field}__isnull": field in missing_fields
                for _, field in RESULT_GROUP_FIELDS
            }
            condition |= models.Q(
                status="incomplete",
                total_score__isnull=True,
                missing_groups=missing_names,
                **field_nullability,
            )
    return condition


class AggregateResult(models.Model):
    class Status(models.TextChoices):
        COMPLETE = "complete", "完整"
        INCOMPLETE = "incomplete", "不完整"

    class IntegrityStatus(models.TextChoices):
        VERIFIED = "verified", "已验证"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(
        "evaluations.EvaluationProject",
        related_name="aggregate_results",
        on_delete=models.PROTECT,
    )
    subject = models.ForeignKey(
        "roster.Employee",
        related_name="evaluation_aggregate_results",
        on_delete=models.PROTECT,
    )
    project_subject = models.ForeignKey(
        "evaluations.ProjectSubject",
        related_name="aggregate_results",
        on_delete=models.PROTECT,
    )
    frozen_subject_public_id = models.UUIDField()
    frozen_subject_name = models.CharField(max_length=200)
    manager_score = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )
    same_department_score = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )
    cross_department_score = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )
    total_score = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=16, choices=Status.choices)
    missing_groups = models.JSONField(default=list)
    valid_submission_count = models.PositiveIntegerField(default=0)
    algorithm_version = models.CharField(max_length=64)
    calculated_at = models.DateTimeField()
    integrity_status = models.CharField(
        max_length=16,
        choices=IntegrityStatus.choices,
        default=IntegrityStatus.VERIFIED,
    )
    input_fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["project_id", "project_subject_id", "algorithm_version"]
        constraints = [
            models.UniqueConstraint(
                fields=["project_subject", "algorithm_version"],
                name="unique_frozen_subject_algorithm_result",
            ),
            models.CheckConstraint(
                condition=_status_and_missing_groups_condition(),
                name="aggregate_status_missing_groups_match",
            ),
            models.CheckConstraint(
                condition=models.Q(integrity_status="verified"),
                name="aggregate_integrity_verified",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(manager_score__isnull=True)
                        | models.Q(manager_score__gte=1, manager_score__lte=5)
                    )
                    & (
                        models.Q(same_department_score__isnull=True)
                        | models.Q(
                            same_department_score__gte=1,
                            same_department_score__lte=5,
                        )
                    )
                    & (
                        models.Q(cross_department_score__isnull=True)
                        | models.Q(
                            cross_department_score__gte=1,
                            cross_department_score__lte=5,
                        )
                    )
                ),
                name="aggregate_group_scores_in_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(total_score__isnull=True)
                    | models.Q(total_score__gte=1, total_score__lte=5)
                ),
                name="aggregate_total_score_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(algorithm_version__regex=r".*\S.*"),
                name="aggregate_algorithm_version_nonempty",
            ),
            models.CheckConstraint(
                condition=models.Q(input_fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="aggregate_fingerprint_lower_hex",
            ),
            models.CheckConstraint(
                condition=models.Q(frozen_subject_name__regex=r".*\S.*"),
                name="aggregate_frozen_subject_name_nonempty",
            ),
        ]
