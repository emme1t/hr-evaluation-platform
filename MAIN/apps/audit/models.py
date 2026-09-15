from django.conf import settings
from django.db import models


AUDIT_ACTION_NAMES = (
    "AUTH_LOGIN_SUCCEEDED",
    "AUTH_ACCESS_DENIED",
    "EMPLOYEE_CREATED",
    "EMPLOYEE_UPDATED",
    "EMPLOYEE_DEACTIVATED",
    "ROSTER_IMPORT_COMMITTED",
    "CATEGORY_CREATED",
    "CATEGORY_UPDATED",
    "CATEGORY_DEACTIVATED",
    "RELATIONSHIP_CREATED",
    "RELATIONSHIP_UPDATED",
    "RELATIONSHIP_DEACTIVATED",
    "RELATIONSHIP_IMPORT_COMMITTED",
    "TEMPLATE_CREATED",
    "TEMPLATE_VERSION_CREATED",
    "TEMPLATE_SEALED",
    "PROJECT_CREATED",
    "PROJECT_PREPARED",
    "PROJECT_LAUNCHED",
    "PROJECT_DEADLINE_EXTENDED",
    "PROJECT_CLOSED",
    "EVALUATION_SUBMITTED",
    "NOTIFICATION_SENT",
    "NOTIFICATION_FAILED",
    "NOTIFICATION_RETRIED",
    "REPORT_SUMMARY_EXPORTED",
    "REPORT_RAW_EXPORTED",
    "REPORT_EXCEPTION_EXPORTED",
    "PROJECT_RECOMPUTED",
    "AUDIT_EXPORTED",
)


class AuditLogImmutableError(RuntimeError):
    pass


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exists():
            raise AuditLogImmutableError("审计日志只能追加，不能修改")
        return super().update(**kwargs)

    def delete(self):
        if self.exists():
            raise AuditLogImmutableError("审计日志只能追加，不能删除")
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        if list(objs):
            raise AuditLogImmutableError("审计日志不能批量修改")
        return 0

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise AuditLogImmutableError("审计日志只能通过校验服务逐条追加")


class AuditLogManager(models.Manager.from_queryset(AuditLogQuerySet)):
    pass


class AuditLog(models.Model):
    objects = AuditLogManager()

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="audit_events",
        on_delete=models.PROTECT,
    )
    effective_role = models.CharField(max_length=24)
    action = models.CharField(
        max_length=48,
        choices=[(name, name) for name in AUDIT_ACTION_NAMES],
    )
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=64)
    target_project_id = models.CharField(max_length=36, blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    correlation_id = models.CharField(max_length=128)
    change_summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "-id"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["target_type", "target_id", "-created_at"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutableError("审计日志只能追加，不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutableError("审计日志只能追加，不能删除")
