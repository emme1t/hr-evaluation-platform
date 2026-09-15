import inspect
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import QuerySet
from django.urls import reverse

from apps.evaluations.models import FormTemplate, TemplateItem
from apps.evaluations.services import templates as template_services
from apps.evaluations.services.templates import (
    TemplateValidationError,
    _raise_template_integrity_error,
    create_initial_template,
    create_template_version,
)
from tests.factories import create_category


def item_values(*, template, order, weight="0.10000"):
    return {
        "template": template,
        "group": "Test group",
        "title": f"Additional fictional item {order}",
        "order": order,
        "weight": weight,
        "score_min": 1,
        "score_max": 5,
        "excellent_description": "Excellent fictional result",
        "good_description": "Good fictional result",
        "qualified_description": "Qualified fictional result",
        "improvement_description": "Improvement fictional result",
    }


def valid_item_payloads():
    return [
        {
            **item_values(template=None, order=1, weight="0.60000"),
            "template": None,
        },
        {
            **item_values(template=None, order=2, weight="0.40000"),
            "template": None,
        },
    ]


def service_items():
    return [
        {key: value for key, value in payload.items() if key != "template"}
        for payload in valid_item_payloads()
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "create_sealed_template",
    [
        lambda category, actor: FormTemplate.objects.create(
            category=category,
            name="Direct sealed fictional template",
            version=1,
            created_by=actor,
            is_sealed=True,
        ),
        lambda category, actor: FormTemplate.objects.bulk_create(
            [
                FormTemplate(
                    category=category,
                    name="Bulk sealed fictional template",
                    version=1,
                    created_by=actor,
                    is_sealed=True,
                )
            ]
        ),
        lambda category, actor: FormTemplate.objects.get_or_create(
            category=category,
            version=1,
            defaults={
                "name": "Get-or-create sealed fictional template",
                "created_by": actor,
                "is_sealed": True,
            },
        ),
        lambda category, actor: FormTemplate.objects.update_or_create(
            category=category,
            version=1,
            defaults={
                "name": "Update-or-create sealed fictional template",
                "created_by": actor,
                "is_sealed": True,
            },
        ),
    ],
    ids=("create", "bulk-create", "get-or-create", "update-or-create"),
)
def test_public_template_creation_paths_cannot_create_sealed_templates(
    hr_admin, create_sealed_template
):
    category = create_category(f"SEALED{uuid4().hex[:8]}", "Sealed Category")

    with pytest.raises(ValidationError, match="版本服务"):
        create_sealed_template(category, hr_admin)

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
def test_direct_model_save_cannot_create_a_sealed_template(hr_admin):
    category = create_category("SEALEDINST", "Sealed Instance Category")

    with pytest.raises(ValidationError, match="版本服务"):
        FormTemplate(
            category=category,
            name="Instance sealed fictional template",
            version=1,
            created_by=hr_admin,
            is_sealed=True,
        ).save()

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
def test_default_and_reverse_template_managers_expose_no_construction_or_seal_capability(
    hr_admin,
):
    category = create_category("MANAGERAPI", "Manager API Category")

    for manager in (FormTemplate.objects, category.formtemplate_set):
        assert not hasattr(manager, "_construct_unsealed")
        assert not hasattr(manager, "_seal_after_validation")
        assert not hasattr(manager, "seal")



@pytest.mark.django_db
def test_caller_set_construction_flag_cannot_bypass_direct_model_save(hr_admin):
    category = create_category("FLAGBYPASS", "Flag Bypass Category")
    template = FormTemplate(
        category=category,
        name="Flag bypass fictional template",
        version=1,
        created_by=hr_admin,
    )
    template._allow_internal_construction = True

    with pytest.raises(ValidationError, match="版本服务"):
        template.save()

    assert FormTemplate.objects.filter(category=category).count() == 0


def build_and_seal_kwargs(category, actor, *, item_payloads=None):
    return {
        "category": category,
        "name": "Atomic fictional template",
        "version": 1,
        "previous_version": None,
        "item_payloads": service_items() if item_payloads is None else item_payloads,
        "actor": actor,
    }


def test_service_module_exposes_only_one_cohesive_template_construction_helper():
    lifecycle_helpers = {
        name
        for name, candidate in inspect.getmembers(template_services, inspect.isfunction)
        if candidate.__module__ == template_services.__name__
        and name.startswith("_")
        and (
            any(token in name for token in ("build", "construct", "persist", "seal"))
            or name == "_create_template"
        )
    }

    assert not hasattr(template_services, "_persist_unsealed_template")
    assert not hasattr(template_services, "_seal_template")
    assert lifecycle_helpers == {"_build_and_seal_template"}
    assert "item_payloads" in inspect.signature(
        template_services._build_and_seal_template
    ).parameters


@pytest.mark.django_db(transaction=True)
def test_private_build_and_seal_requires_an_hr_actor_and_an_atomic_transaction(
    hr_admin, user_with_role
):
    category = create_category("BUILDCONTEXT", "Build Context Category")

    with transaction.atomic():
        with pytest.raises(ValueError, match="HR"):
            template_services._build_and_seal_template(
                **build_and_seal_kwargs(category, user_with_role)
            )
    with pytest.raises(RuntimeError, match="事务"):
        template_services._build_and_seal_template(
            **build_and_seal_kwargs(category, hr_admin)
        )

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "write_item",
    [
        lambda template: TemplateItem(**item_values(template=template, order=1)).save(),
        lambda template: TemplateItem.objects.create(
            **item_values(template=template, order=1)
        ),
        lambda template: template.items.create(
            **{
                key: value
                for key, value in item_values(template=template, order=1).items()
                if key != "template"
            }
        ),
        lambda template: TemplateItem.objects.bulk_create(
            [TemplateItem(**item_values(template=template, order=1))]
        ),
    ],
    ids=("instance-save", "manager-create", "related-create", "bulk-create"),
)
def test_every_item_insert_path_locks_its_template_row(
    monkeypatch, template_v1, write_item
):
    locked_models = []
    original_select_for_update = QuerySet.select_for_update

    def record_select_for_update(self, *args, **kwargs):
        if self.model is FormTemplate:
            locked_models.append(self.model)
        return original_select_for_update(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)

    with pytest.raises(ValidationError, match="密封"):
        write_item(template_v1)

    assert locked_models == [FormTemplate]


@pytest.mark.django_db
def test_initial_and_version_services_share_category_then_template_chain_locks(
    monkeypatch, hr_admin
):
    lock_events = []
    original_lock_category = template_services._lock_category
    original_lock_template_chain = template_services._lock_template_chain

    def record_lock_category(*args, **kwargs):
        lock_events.append("category")
        return original_lock_category(*args, **kwargs)

    def record_lock_template_chain(*args, **kwargs):
        lock_events.append("template-chain")
        return original_lock_template_chain(*args, **kwargs)

    monkeypatch.setattr(template_services, "_lock_category", record_lock_category)
    monkeypatch.setattr(
        template_services, "_lock_template_chain", record_lock_template_chain
    )
    initial_category = create_category("INITLOCK", "Initial Lock Category")
    initial_template = create_initial_template(
        category=initial_category,
        name="Initial lock fictional template",
        items=service_items(),
        actor=hr_admin,
    )
    initial_path_events = lock_events[:]
    lock_events.clear()
    template_v2 = create_template_version(
        initial_template,
        changes={
            "name": "Version lock fictional template",
            "items": initial_template.item_payloads(),
        },
        actor=hr_admin,
    )

    assert initial_path_events == ["category", "template-chain"]
    assert lock_events == ["category", "template-chain"]
    assert template_v2.version == 2


@pytest.mark.django_db
def test_version_service_uses_source_category_before_any_template_row_lock(
    monkeypatch, template_v1, hr_admin
):
    category_locked = False
    original_filter = FormTemplate.objects.filter
    original_lock_category = template_services._lock_category

    def record_category_lock(*args, **kwargs):
        nonlocal category_locked
        category = original_lock_category(*args, **kwargs)
        category_locked = True
        return category

    def reject_prelock_template_query(*args, **kwargs):
        if not category_locked:
            raise AssertionError(
                "version service queried a template before locking its category"
            )
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(template_services, "_lock_category", record_category_lock)
    monkeypatch.setattr(
        FormTemplate.objects, "filter", reject_prelock_template_query
    )

    template_v2 = create_template_version(
        template_v1,
        changes={
            "name": "Category-first version",
            "items": template_v1.item_payloads(),
        },
        actor=hr_admin,
    )

    assert template_v2.is_sealed is True


@pytest.mark.django_db
def test_template_chain_lock_returns_every_version_in_primary_key_order(
    template_v1, hr_admin
):
    template_v2 = create_template_version(
        template_v1,
        changes={"name": "Chain lock version two", "items": template_v1.item_payloads()},
        actor=hr_admin,
    )

    locked_templates = template_services._lock_template_chain(template_v1.category)

    assert [template.pk for template in locked_templates] == sorted(
        [template_v1.pk, template_v2.pk]
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "write_item",
    [
        lambda template: TemplateItem(**item_values(template=template, order=9)).save(),
        lambda template: TemplateItem.objects.create(
            **item_values(template=template, order=9)
        ),
        lambda template: template.items.create(
            **{
                key: value
                for key, value in item_values(template=template, order=9).items()
                if key != "template"
            }
        ),
        lambda template: TemplateItem.objects.bulk_create(
            [TemplateItem(**item_values(template=template, order=9))]
        ),
        lambda template: TemplateItem.objects.filter(template=template).bulk_create(
            [TemplateItem(**item_values(template=template, order=9))]
        ),
    ],
    ids=(
        "instance-save",
        "manager-create",
        "related-manager-create",
        "manager-bulk-create",
        "queryset-bulk-create",
    ),
)
def test_sealed_template_rejects_every_item_append_path(template_v1, write_item):
    with pytest.raises(ValidationError, match="密封"):
        write_item(template_v1)

    assert template_v1.items.count() == 2


@pytest.mark.django_db
def test_template_services_construct_and_seal_complete_templates(hr_admin):
    category = create_category("SEAL", "Sealed Template Category")

    template = create_initial_template(
        category=category,
        name="Sealed fictional template",
        items=service_items(),
        actor=hr_admin,
    )

    assert getattr(template, "is_sealed", False) is True
    assert template.items.count() == 2
    assert validate_total(template) == Decimal("1.00000")


@pytest.mark.django_db
def test_atomic_build_and_seal_returns_only_a_refreshed_sealed_template(hr_admin):
    category = create_category("DIRECTBUILD", "Direct Build Category")

    with transaction.atomic():
        template = template_services._build_and_seal_template(
            **build_and_seal_kwargs(category, hr_admin)
        )

    stored_template = FormTemplate.objects.get(pk=template.pk)
    assert template.is_sealed is True
    assert stored_template.is_sealed is True
    assert template.items.count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("invalid_item_payloads", "expected_code"),
    [
        (
            lambda: [
                service_items()[0],
                {**service_items()[1], "weight": Decimal("0.30000")},
            ],
            "INVALID",
        ),
        (
            lambda: [
                service_items()[0],
                {**service_items()[1], "order": 1},
            ],
            "ITEM_ORDER_CONFLICT",
        ),
    ],
    ids=("validation-failure", "persistence-failure"),
)
def test_atomic_build_and_seal_rolls_back_every_row_when_construction_fails(
    hr_admin, invalid_item_payloads, expected_code
):
    category = create_category(
        f"BUILDFAIL{uuid4().hex[:8]}", "Atomic Build Failure Category"
    )

    with transaction.atomic():
        with pytest.raises(TemplateValidationError) as raised:
            template_services._build_and_seal_template(
                **build_and_seal_kwargs(
                    category, hr_admin, item_payloads=invalid_item_payloads()
                )
            )
        assert raised.value.code == expected_code
        assert FormTemplate.objects.filter(category=category).count() == 0
        assert TemplateItem.objects.filter(template__category=category).count() == 0

    assert FormTemplate.objects.filter(category=category).count() == 0
    assert TemplateItem.objects.filter(template__category=category).count() == 0


def validate_total(template):
    return sum((item.weight for item in template.items.all()), Decimal("0.00000"))


@pytest.mark.django_db
def test_template_item_order_has_a_database_constraint(hr_admin):
    category = create_category("ORD", "Order Constraint Category")
    template = FormTemplate(
        category=category,
        name="Unsealed fictional template",
        version=1,
        previous_version=None,
        created_by=hr_admin,
    )
    models.Model.save(template, force_insert=True, using=FormTemplate.objects.db)
    TemplateItem.objects.create(**item_values(template=template, order=1))

    with transaction.atomic():
        with pytest.raises(IntegrityError):
            TemplateItem.objects.create(**item_values(template=template, order=1))


@pytest.mark.django_db
def test_template_services_reject_non_hr_actors(user_with_role):
    category = create_category("AUTH", "Authorization Category")

    with pytest.raises(ValueError, match="HR"):
        create_initial_template(
            category=category,
            name="Unauthorized fictional template",
            items=service_items(),
            actor=user_with_role,
        )

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
def test_template_version_service_rejects_non_hr_actors(template_v1, user_with_role):
    with pytest.raises(ValueError, match="HR"):
        create_template_version(
            template_v1,
            changes={"name": "Unauthorized version", "items": template_v1.item_payloads()},
            actor=user_with_role,
        )

    assert FormTemplate.objects.filter(category=template_v1.category).count() == 1


@pytest.mark.django_db
def test_stale_template_source_cannot_branch_the_version_chain(template_v1, hr_admin):
    template_v2 = create_template_version(
        template_v1,
        changes={"name": "Fictional version two", "items": template_v1.item_payloads()},
        actor=hr_admin,
    )

    with pytest.raises(TemplateValidationError, match="最新"):
        create_template_version(
            template_v1,
            changes={"name": "Stale fictional branch", "items": template_v1.item_payloads()},
            actor=hr_admin,
        )

    assert template_v2.previous_version_id == template_v1.id
    assert FormTemplate.objects.filter(category=template_v1.category).count() == 2


@pytest.mark.django_db
def test_version_uniqueness_collision_is_translated_without_poisoning_transaction(
    monkeypatch, template_v1, hr_admin
):
    original_lock_template_chain = template_services._lock_template_chain

    def return_stale_latest(category):
        locked_templates = original_lock_template_chain(category)
        locked_templates[0].version = 0
        return locked_templates

    monkeypatch.setattr(
        template_services, "_lock_template_chain", return_stale_latest
    )

    with transaction.atomic():
        with pytest.raises(TemplateValidationError, match="版本"):
            create_template_version(
                template_v1,
                changes={"name": "Racing fictional version", "items": template_v1.item_payloads()},
                actor=hr_admin,
            )
        follow_up = create_category("POSTRACE", "Post-race Category")

    assert follow_up.code == "POSTRACE"
    assert FormTemplate.objects.filter(category=template_v1.category).count() == 1


def test_postgresql_version_constraint_name_is_translated_stably():
    with pytest.raises(TemplateValidationError, match="版本") as raised:
        _raise_template_integrity_error(
            IntegrityError(
                'duplicate key value violates unique constraint "unique_category_template_version"'
            )
        )

    assert raised.value.code == "TEMPLATE_VERSION_CONFLICT"


@pytest.mark.django_db
def test_seal_failure_rolls_back_every_new_template_record(monkeypatch, hr_admin):
    category = create_category("SEALFAIL", "Seal Failure Category")
    original_model_save = models.Model.save

    def reject_seal(instance, *args, **kwargs):
        if isinstance(instance, FormTemplate) and kwargs.get("update_fields") == {
            "is_sealed"
        }:
            raise ValidationError("密封失败")
        return original_model_save(instance, *args, **kwargs)

    monkeypatch.setattr(models.Model, "save", reject_seal)

    with pytest.raises(TemplateValidationError, match="密封"):
        create_initial_template(
            category=category,
            name="Unsealable fictional template",
            items=service_items(),
            actor=hr_admin,
        )

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
def test_invalid_template_edit_rolls_back_every_new_item_and_unsealed_version(
    client, hr_admin, template_v1
):
    client.force_login(hr_admin)
    items = template_v1.item_payloads()
    items[1]["weight"] = Decimal("0.30000")
    data = {
        "name": "Invalid fictional version",
        "items-TOTAL_FORMS": "2",
        "items-INITIAL_FORMS": "2",
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "1000",
    }
    for index, item in enumerate(items):
        for field, value in item.items():
            data[f"items-{index}-{field}"] = (
                format(value, "f") if isinstance(value, Decimal) else value
            )

    response = client.post(
        reverse("evaluations:template-edit", args=[template_v1.public_id]), data
    )

    assert response.status_code == 400
    assert FormTemplate.objects.filter(category=template_v1.category).count() == 1
    assert template_v1.items.count() == 2
    assert all(
        getattr(template, "is_sealed", False)
        for template in FormTemplate.objects.filter(category=template_v1.category)
    )
