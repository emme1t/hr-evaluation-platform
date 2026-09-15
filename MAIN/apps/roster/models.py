import uuid

from django.conf import settings
from django.db import models


class EmployeeCategory(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)


class Employee(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    employee_no = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    corporate_email = models.EmailField(unique=True)
    department_level_1 = models.CharField(max_length=120)
    department_level_2 = models.CharField(max_length=120)
    category = models.ForeignKey(EmployeeCategory, on_delete=models.PROTECT)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    wecom_userid = models.CharField(max_length=128, unique=True, null=True, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    is_active = models.BooleanField(default=True)


class EvaluationRelationship(models.Model):
    class Type(models.TextChoices):
        MANAGER = "manager", "上级"
        SAME_DEPARTMENT = "same_department", "同部门协作"
        CROSS_DEPARTMENT = "cross_department", "跨部门协作"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    subject = models.ForeignKey(
        Employee,
        related_name="evaluation_subject_relations",
        on_delete=models.PROTECT,
    )
    evaluator = models.ForeignKey(
        Employee, related_name="evaluation_assignments", on_delete=models.PROTECT
    )
    relationship_type = models.CharField(max_length=32, choices=Type.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "evaluator", "relationship_type"],
                condition=models.Q(is_active=True),
                name="unique_active_evaluation_relationship",
            )
        ]


class ImportBatch(models.Model):
    class Type(models.TextChoices):
        ROSTER = "roster", "花名册"
        RELATIONSHIP = "relationship", "协作关系"

    class Status(models.TextChoices):
        PREVIEWED = "previewed", "已预检"
        COMMITTED = "committed", "已提交"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    import_type = models.CharField(max_length=32, choices=Type.choices)
    original_filename = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64)
    rows = models.JSONField(default=list)
    preview = models.JSONField(default=dict)
    valid_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    issue_count = models.PositiveIntegerField(default=0)
    create_count = models.PositiveIntegerField(default=0)
    update_count = models.PositiveIntegerField(default=0)
    skip_count = models.PositiveIntegerField(default=0)
    deactivate_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PREVIEWED
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_import_batches",
        on_delete=models.PROTECT,
    )
    committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="committed_import_batches",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    commit_mode = models.CharField(max_length=16, blank=True)
    duplicate_policy = models.CharField(max_length=16, blank=True)


class ImportIssue(models.Model):
    batch = models.ForeignKey(
        ImportBatch, related_name="issues", on_delete=models.CASCADE
    )
    row_number = models.PositiveIntegerField()
    code = models.CharField(max_length=64)
    message = models.CharField(max_length=240)

    class Meta:
        ordering = ["row_number", "id"]
