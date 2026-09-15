from decimal import Decimal

import pytest

from apps.evaluations.models import FormTemplate
from apps.evaluations.services.templates import (
    TemplateValidationError,
    create_initial_template,
)
from tests.factories import create_category

from apps.evaluations.services.templates import validate_template


@pytest.mark.django_db
def test_template_item_weights_must_sum_to_one(template_factory):
    template = template_factory(item_weights=("0.60", "0.30"))

    errors = validate_template(template)

    assert errors == ["评价项权重合计必须为 100%"]


@pytest.mark.django_db
def test_template_requires_at_least_one_item(template_factory):
    template = template_factory(item_weights=())

    assert validate_template(template) == ["评价表至少需要一个评价项"]


@pytest.mark.django_db
def test_template_service_rejects_duplicate_item_order_before_sealing(hr_admin):
    category = create_category("DUP", "Duplicate Order Category")
    items = [
        {
            "group": "Test group",
            "title": "First fictional item",
            "order": 1,
            "weight": "0.60",
            "score_min": 1,
            "score_max": 5,
            "excellent_description": "Excellent fictional result",
            "good_description": "Good fictional result",
            "qualified_description": "Qualified fictional result",
            "improvement_description": "Improvement fictional result",
        },
        {
            "group": "Test group",
            "title": "Second fictional item",
            "order": 1,
            "weight": "0.40",
            "score_min": 1,
            "score_max": 5,
            "excellent_description": "Excellent fictional result",
            "good_description": "Good fictional result",
            "qualified_description": "Qualified fictional result",
            "improvement_description": "Improvement fictional result",
        },
    ]

    with pytest.raises(TemplateValidationError, match="序号"):
        create_initial_template(
            category=category,
            name="Duplicate fictional template",
            items=items,
            actor=hr_admin,
        )

    assert FormTemplate.objects.filter(category=category).count() == 0


@pytest.mark.django_db
def test_template_rejects_blank_item_title(template_factory):
    template = template_factory(item_overrides={1: {"title": "   "}})

    assert validate_template(template) == ["评价项标题不能为空"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [
        {"score_min": 0, "score_max": 5},
        {"score_min": 1, "score_max": 6},
        {"score_min": 5, "score_max": 1},
    ],
)
def test_template_rejects_invalid_score_range(template_factory, overrides):
    template = template_factory(item_overrides={1: overrides})

    assert validate_template(template) == ["评价项评分范围无效"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "description_field",
    [
        "excellent_description",
        "good_description",
        "qualified_description",
        "improvement_description",
    ],
)
def test_template_requires_all_four_descriptions(template_factory, description_field):
    template = template_factory(item_overrides={1: {description_field: " "}})

    assert validate_template(template) == ["评价项四档描述均为必填项"]


@pytest.mark.django_db
def test_template_weight_total_uses_exact_decimal_arithmetic(template_factory):
    template = template_factory(item_weights=("0.33333", "0.66666"))

    assert validate_template(template) == ["评价项权重合计必须为 100%"]
    assert Decimal("0.33333") + Decimal("0.66667") == Decimal("1.00000")


@pytest.mark.django_db
def test_template_validation_reports_every_invalid_requirement(template_factory):
    template = template_factory(
        item_weights=("0.60", "0.30"),
        item_overrides={
            1: {
                "title": " ",
                "excellent_description": " ",
                "score_min": 0,
            }
        },
    )

    assert validate_template(template) == [
        "评价项标题不能为空",
        "评价项评分范围无效",
        "评价项四档描述均为必填项",
        "评价项权重合计必须为 100%",
    ]
