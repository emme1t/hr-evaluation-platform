import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.roster.models import EvaluationRelationship


TASK_ASSOCIATION_FIELDS = {
    "project",
    "project_id",
    "project_subject",
    "project_subject_id",
    "subject",
    "subject_id",
}


def validate_task_associations(tasks):
    tasks = list(tasks)
    missing_project_subject_ids = {
        task.project_subject_id
        for task in tasks
        if task.project_subject_id is not None
        and "project_subject" not in task._state.fields_cache
    }
    project_subjects = {
        project_subject.pk: project_subject
        for project_subject in ProjectSubject.objects.filter(
            pk__in=missing_project_subject_ids
        )
    }
    for task in tasks:
        project_subject = task._state.fields_cache.get("project_subject")
        if project_subject is None:
            project_subject = project_subjects.get(task.project_subject_id)
        if project_subject is None:
            raise ValidationError(
                "任务被评价人快照不存在", code="task_project_subject_missing"
            )
        if task.project_id != project_subject.project_id:
            raise ValidationError(
                "任务项目关联不一致", code="task_project_mismatch"
            )
        if task.subject_id != project_subject.subject_id:
            raise ValidationError(
                "任务被评价人关联不一致", code="task_subject_mismatch"
            )
        if task.relationship_type not in EvaluationRelationship.Type.values:
            raise ValidationError(
                "任务关系类型无效", code="task_relationship_type_invalid"
            )


class EvaluationTaskQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if TASK_ASSOCIATION_FIELDS.intersection(kwargs):
            raise ValidationError(
                "任务关联字段不能通过批量更新修改",
                code="task_association_bulk_update_forbidden",
            )
        return super().update(**kwargs)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objects = list(objs)
        validate_task_associations(objects)
        return super().bulk_create(
            objects,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if TASK_ASSOCIATION_FIELDS.intersection(fields):
            validate_task_associations(objects)
        return super().bulk_update(objects, fields, batch_size=batch_size)


class EvaluationTaskManager(models.Manager.from_queryset(EvaluationTaskQuerySet)):
    pass


class EvaluationProject(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        READY = "ready", "待发送"
        ACTIVE = "active", "进行中"
        CLOSED = "closed", "已截止"
        ARCHIVED = "archived", "已归档"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    deadline = models.DateTimeField()
    rule_snapshot = models.JSONField(default=dict)
    summary_template = models.ForeignKey(
        "reporting.SummaryWorkbookTemplate",
        related_name="projects",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    summary_template_snapshot = models.JSONField(default=dict)
    reporting_snapshot = models.JSONField(default=dict)
    prepared_at = models.DateTimeField(null=True, blank=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ProjectSubject(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(
        EvaluationProject, related_name="subjects", on_delete=models.PROTECT
    )
    subject = models.ForeignKey(
        "roster.Employee",
        related_name="evaluation_project_memberships",
        on_delete=models.PROTECT,
    )
    template = models.ForeignKey(
        "evaluations.FormTemplate",
        related_name="project_subjects",
        on_delete=models.PROTECT,
    )
    subject_snapshot = models.JSONField(default=dict)
    template_snapshot = models.JSONField(default=dict)
    relationship_snapshot = models.JSONField(default=list)
    reporting_snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "subject"],
                name="unique_project_subject",
            )
        ]


class EvaluationTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待提交"
        SUBMITTED = "submitted", "已提交"
        NOT_SUBMITTED = "not_submitted", "未提交"

    objects = EvaluationTaskManager()
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(
        EvaluationProject, related_name="tasks", on_delete=models.PROTECT
    )
    project_subject = models.ForeignKey(
        ProjectSubject, related_name="tasks", on_delete=models.PROTECT
    )
    evaluator = models.ForeignKey(
        "roster.Employee",
        related_name="tasks_to_complete",
        on_delete=models.PROTECT,
    )
    subject = models.ForeignKey(
        "roster.Employee",
        related_name="evaluation_tasks",
        on_delete=models.PROTECT,
    )
    relationship_type = models.CharField(
        max_length=32, choices=EvaluationRelationship.Type.choices
    )
    relationship_snapshot_item_id = models.UUIDField(default=uuid.uuid4)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "evaluator", "subject", "relationship_type"],
                name="unique_project_evaluation_task",
            ),
            models.UniqueConstraint(
                fields=["project_subject", "relationship_snapshot_item_id"],
                name="unique_project_relation_snapshot_task",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    relationship_type__in=EvaluationRelationship.Type.values
                ),
                name="valid_evaluation_task_relationship_type",
            ),
        ]

    def clean(self):
        super().clean()
        validate_task_associations([self])

    def save(self, *args, **kwargs):
        validate_task_associations([self])
        return super().save(*args, **kwargs)
