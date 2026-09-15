import uuid

from django.db import models

from .projects import EvaluationTask


class Submission(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task = models.ForeignKey(
        EvaluationTask, related_name="submissions", on_delete=models.PROTECT
    )
    is_final = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    saved_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    client_type = models.CharField(max_length=32, default="web")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["task"],
                condition=models.Q(is_final=True),
                name="one_final_submission_per_task",
            ),
            models.UniqueConstraint(
                fields=["task", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_task_idempotency_key",
            ),
        ]


class SubmissionAnswer(models.Model):
    submission = models.ForeignKey(
        Submission, related_name="answers", on_delete=models.CASCADE
    )
    item_snapshot_id = models.UUIDField()
    score = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "item_snapshot_id"],
                name="unique_submission_snapshot_answer",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=1, score__lte=5),
                name="submission_answer_score_1_to_5",
            ),
        ]
