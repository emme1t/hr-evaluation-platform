from contextlib import contextmanager
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction


ITEM_PAYLOAD_FIELDS = (
    "group",
    "title",
    "order",
    "weight",
    "score_min",
    "score_max",
    "excellent_description",
    "good_description",
    "qualified_description",
    "improvement_description",
)


class ImmutableVersionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exists():
            raise ValidationError("已保存的模板版本不能修改")
        return super().update(**kwargs)

    def delete(self):
        if self.exists():
            raise ValidationError("已保存的模板版本不能删除")
        return super().delete()


class ImmutableVersionManager(models.Manager.from_queryset(ImmutableVersionQuerySet)):
    pass


def _raise_public_template_creation_error():
    raise ValidationError("评价表模板必须通过版本服务创建")


class FormTemplateQuerySet(ImmutableVersionQuerySet):
    def create(self, **kwargs):
        _raise_public_template_creation_error()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        _raise_public_template_creation_error()

    def get_or_create(self, defaults=None, **kwargs):
        _raise_public_template_creation_error()

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        _raise_public_template_creation_error()


class FormTemplateManager(models.Manager.from_queryset(FormTemplateQuerySet)):
    pass


class ImmutableVersionModel(models.Model):
    """Prevent saved template records from being changed or removed in place."""

    objects = ImmutableVersionManager()
    immutable_fields: tuple[str, ...] = ()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self)._default_manager.filter(pk=self.pk).values(
                *self.immutable_fields
            ).first()
            if original and any(
                original[field] != getattr(self, field) for field in self.immutable_fields
            ):
                raise ValidationError("已保存的模板版本不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("已保存的模板版本不能删除")


class FormTemplate(ImmutableVersionModel):
    objects = FormTemplateManager()
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    category = models.ForeignKey("roster.EmployeeCategory", on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    version = models.PositiveIntegerField()
    previous_version = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="next_versions",
        on_delete=models.PROTECT,
    )
    is_active = models.BooleanField(default=True)
    is_sealed = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    immutable_fields = (
        "public_id",
        "category_id",
        "name",
        "version",
        "previous_version_id",
        "is_active",
        "is_sealed",
        "created_by_id",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "version"],
                name="unique_category_template_version",
            )
        ]
        ordering = ["category_id", "version"]
        base_manager_name = "objects"
        default_manager_name = "objects"

    def item_payloads(self):
        return [
            {field: getattr(item, field) for field in ITEM_PAYLOAD_FIELDS}
            for item in self.items.order_by("order", "pk")
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            _raise_public_template_creation_error()
        return super().save(*args, **kwargs)


@contextmanager
def _locked_unsealed_template_rows(template_ids):
    """Hold template row locks while items are appended to them.

    The ordered lock acquisition is shared with direct saves and bulk writes, so
    a PostgreSQL seal cannot validate one item set and then accept a later append.
    """
    ids = sorted(
        {template_id for template_id in template_ids if template_id is not None}
    )
    with transaction.atomic():
        templates = list(
            FormTemplate.objects.select_for_update(of=("self",))
            .filter(pk__in=ids)
            .order_by("pk")
        )
        if any(template.is_sealed for template in templates):
            raise ValidationError("密封的评价表模板不能新增评价项")
        yield templates


class TemplateItemQuerySet(ImmutableVersionQuerySet):
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
        with _locked_unsealed_template_rows(item.template_id for item in objects):
            return super().bulk_create(
                objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )


class TemplateItemManager(models.Manager.from_queryset(TemplateItemQuerySet)):
    pass


class TemplateItem(ImmutableVersionModel):
    objects = TemplateItemManager()
    template = models.ForeignKey(
        FormTemplate, related_name="items", on_delete=models.CASCADE
    )
    group = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    order = models.PositiveIntegerField()
    weight = models.DecimalField(max_digits=6, decimal_places=5)
    score_min = models.PositiveSmallIntegerField(default=1)
    score_max = models.PositiveSmallIntegerField(default=5)
    excellent_description = models.TextField()
    good_description = models.TextField()
    qualified_description = models.TextField()
    improvement_description = models.TextField()
    immutable_fields = (
        "template_id",
        "group",
        "title",
        "order",
        "weight",
        "score_min",
        "score_max",
        "excellent_description",
        "good_description",
        "qualified_description",
        "improvement_description",
    )

    class Meta:
        ordering = ["order", "id"]
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["template", "order"],
                name="unique_template_item_order",
            )
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            with _locked_unsealed_template_rows([self.template_id]):
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)
