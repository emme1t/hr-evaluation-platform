import uuid

from django.db import models


class NotificationOutbox(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "待发送"
        SENT = "sent", "已发送"
        FAILED = "failed", "失败"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(
        "evaluations.EvaluationProject",
        related_name="notification_outboxes",
        on_delete=models.PROTECT,
    )
    recipient = models.ForeignKey(
        "roster.Employee",
        related_name="notification_outboxes",
        on_delete=models.PROTECT,
    )
    tasks = models.ManyToManyField(
        "evaluations.EvaluationTask", related_name="notification_outboxes"
    )
    task_count = models.PositiveIntegerField()
    task_digest = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "recipient"],
                name="unique_project_notification_recipient",
            ),
            models.CheckConstraint(
                condition=models.Q(task_count__gte=1),
                name="notification_outbox_task_count_positive",
            ),
        ]


class NotificationAttempt(models.Model):
    class Channel(models.TextChoices):
        WECOM = "wecom", "企业微信"
        EMAIL = "email", "企业邮箱"

    class Status(models.TextChoices):
        QUEUED = "queued", "待发送"
        SENT = "sent", "已发送"
        FAILED = "failed", "失败"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    outbox = models.ForeignKey(
        NotificationOutbox, related_name="attempts", on_delete=models.PROTECT
    )
    project = models.ForeignKey(
        "evaluations.EvaluationProject",
        related_name="notification_attempts",
        on_delete=models.PROTECT,
    )
    recipient = models.ForeignKey(
        "roster.Employee",
        related_name="notification_attempts",
        on_delete=models.PROTECT,
    )
    tasks = models.ManyToManyField(
        "evaluations.EvaluationTask", related_name="notification_attempts"
    )
    task_count = models.PositiveIntegerField()
    channel = models.CharField(max_length=16, choices=Channel.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    failure_code = models.CharField(max_length=80, blank=True)
    attempt = models.PositiveIntegerField()
    payload_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["outbox", "attempt"],
                name="unique_notification_attempt_number",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt__gte=1),
                name="notification_attempt_number_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(task_count__gte=1),
                name="notification_attempt_task_count_positive",
            ),
        ]


# Historical Task 10 public name. The Contract implementation stores each log row
# as an append-only attempt attached to a stable logical outbox.
NotificationLog = NotificationAttempt


class EmailMagicLink(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    recipient = models.ForeignKey(
        "roster.Employee",
        related_name="email_magic_links",
        on_delete=models.PROTECT,
    )
    project = models.ForeignKey(
        "evaluations.EvaluationProject",
        related_name="email_magic_links",
        on_delete=models.PROTECT,
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["expires_at", "used_at", "revoked_at"])]
