from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.roster.models import EmployeeCategory


INITIAL_MIGRATION = ("evaluations", "0001_initial")
SEALED_MIGRATION = ("evaluations", "0002_alter_formtemplate_options_and_more")


def migrate_evaluations_to(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def legacy_item_values(*, template_id, order, weight, **overrides):
    values = {
        "template_id": template_id,
        "group": "Legacy fictional group",
        "title": f"Legacy fictional item {order}",
        "order": order,
        "weight": Decimal(weight),
        "score_min": 1,
        "score_max": 5,
        "excellent_description": "Excellent fictional result",
        "good_description": "Good fictional result",
        "qualified_description": "Qualified fictional result",
        "improvement_description": "Improvement fictional result",
    }
    values.update(overrides)
    return values


def create_legacy_template(*, apps, category_id, user_id, name, version=1):
    legacy_template = apps.get_model("evaluations", "FormTemplate")
    return legacy_template.objects.create(
        category_id=category_id,
        name=name,
        version=version,
        created_by_id=user_id,
    )


@pytest.mark.django_db(transaction=True)
def test_invalid_legacy_templates_stop_before_their_seal_or_item_constraint():
    current_leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    legacy_apps = migrate_evaluations_to(INITIAL_MIGRATION)
    legacy_item = legacy_apps.get_model("evaluations", "TemplateItem")
    user = get_user_model().objects.create_user(username="legacy-gate@example.test")
    category = EmployeeCategory.objects.create(
        code="LEGACYGATE", name="Legacy Gate Category"
    )
    missing_items = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy missing items",
    )
    duplicate_orders = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy duplicate orders",
        version=2,
    )
    invalid_scores = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy invalid scores",
        version=3,
    )
    blank_required_fields = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy blank fields",
        version=4,
    )
    invalid_weight_total = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy invalid weights",
        version=5,
    )
    legacy_item.objects.bulk_create(
        [
            legacy_item(**legacy_item_values(template_id=duplicate_orders.pk, order=1, weight="0.50000")),
            legacy_item(**legacy_item_values(template_id=duplicate_orders.pk, order=1, weight="0.50000")),
            legacy_item(
                **legacy_item_values(
                    template_id=invalid_scores.pk,
                    order=1,
                    weight="1.00000",
                    score_min=0,
                )
            ),
            legacy_item(
                **legacy_item_values(
                    template_id=blank_required_fields.pk,
                    order=1,
                    weight="1.00000",
                    title="  ",
                    excellent_description=" ",
                )
            ),
            legacy_item(**legacy_item_values(template_id=invalid_weight_total.pk, order=1, weight="0.60000")),
            legacy_item(**legacy_item_values(template_id=invalid_weight_total.pk, order=2, weight="0.30000")),
        ]
    )

    try:
        with pytest.raises(RuntimeError, match="评价表历史数据预检失败") as raised:
            migrate_evaluations_to(SEALED_MIGRATION)

        message = str(raised.value)
        assert "invalid_template_count=5" in message
        assert "duplicate_order_template_count=1" in message
        assert "Legacy missing items" not in message
        assert not MigrationRecorder.Migration.objects.filter(
            app="evaluations", name=SEALED_MIGRATION[1]
        ).exists()
        constraint_names = connection.introspection.get_constraints(
            connection.cursor(), "evaluations_templateitem"
        )
        assert "unique_template_item_order" not in constraint_names
    finally:
        legacy_item.objects.all().delete()
        legacy_apps.get_model("evaluations", "FormTemplate").objects.all().delete()
        MigrationExecutor(connection).migrate(current_leaves)


@pytest.mark.django_db(transaction=True)
def test_valid_legacy_templates_migrate_sealed_with_the_item_order_constraint():
    current_leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    legacy_apps = migrate_evaluations_to(INITIAL_MIGRATION)
    legacy_item = legacy_apps.get_model("evaluations", "TemplateItem")
    user = get_user_model().objects.create_user(username="legacy-valid@example.test")
    category = EmployeeCategory.objects.create(
        code="LEGACYVALID", name="Legacy Valid Category"
    )
    legacy_template = create_legacy_template(
        apps=legacy_apps,
        category_id=category.pk,
        user_id=user.pk,
        name="Legacy valid template",
    )
    legacy_item.objects.bulk_create(
        [
            legacy_item(**legacy_item_values(template_id=legacy_template.pk, order=1, weight="0.60000")),
            legacy_item(**legacy_item_values(template_id=legacy_template.pk, order=2, weight="0.40000")),
        ]
    )

    try:
        sealed_apps = migrate_evaluations_to(SEALED_MIGRATION)
        sealed_template = sealed_apps.get_model("evaluations", "FormTemplate")
        sealed_item = sealed_apps.get_model("evaluations", "TemplateItem")

        assert sealed_template.objects.get(pk=legacy_template.pk).is_sealed is True
        with transaction.atomic():
            with pytest.raises(IntegrityError):
                sealed_item.objects.create(
                    **legacy_item_values(
                        template_id=legacy_template.pk,
                        order=1,
                        weight="0.10000",
                    )
                )
    finally:
        MigrationExecutor(connection).migrate(current_leaves)
