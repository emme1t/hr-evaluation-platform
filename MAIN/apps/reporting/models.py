import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ImmutableReportingQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exists():
            raise ValidationError("已登记的汇总模板版本不能修改")
        return super().update(**kwargs)

    def delete(self):
        if self.exists():
            raise ValidationError("已登记的汇总模板版本不能删除")
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if objects:
            raise ValidationError("已登记的汇总模板版本不能修改")
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError("已登记的汇总模板版本不能修改")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class SummaryWorkbookTemplate(models.Model):
    objects = models.Manager.from_queryset(ImmutableReportingQuerySet)()

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    filename = models.CharField(max_length=180)
    version = models.PositiveIntegerField()
    storage_path = models.CharField(max_length=240, unique=True)
    sha256 = models.CharField(max_length=64)
    structure_signature = models.CharField(max_length=64)
    structure_manifest = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="summary_workbook_templates",
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["filename", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["filename", "version"],
                name="unique_summary_template_filename_version",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self)._base_manager.filter(pk=self.pk).values().first()
            if original:
                immutable = {
                    "public_id",
                    "filename",
                    "version",
                    "storage_path",
                    "sha256",
                    "structure_signature",
                    "structure_manifest",
                    "is_active",
                    "created_by_id",
                    "created_at",
                }
                if any(original[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("已登记的汇总模板版本不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("已登记的汇总模板版本不能删除")
