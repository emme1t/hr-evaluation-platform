import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models
from django.test import Client
from django.urls import reverse

from apps.evaluations.models import FormTemplate
from apps.evaluations.services.templates import create_template_version
from tests.factories import create_category


ITEM_FIELDS = (
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


def formset_post_data(*, name, items, initial_forms):
    data = {
        "name": name,
        "items-TOTAL_FORMS": str(len(items)),
        "items-INITIAL_FORMS": str(initial_forms),
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "1000",
    }
    for index, item in enumerate(items):
        for field in ITEM_FIELDS:
            value = item[field]
            data[f"items-{index}-{field}"] = (
                format(value, "f") if isinstance(value, Decimal) else value
            )
    return data


@pytest.mark.django_db
def test_editing_template_creates_new_version_and_preserves_source(template_v1, hr_admin):
    changed_items = template_v1.item_payloads()
    changed_items[0]["title"] = "Updated fictional item"

    template_v2 = create_template_version(
        template_v1,
        changes={"name": "Fictional HR template 2026", "items": changed_items},
        actor=hr_admin,
    )

    template_v1.refresh_from_db()
    assert template_v1.version == 1
    assert template_v1.name == "Test evaluation template v1"
    assert template_v1.items.get(order=1).title == "Test item 1"
    assert template_v2.version == 2
    assert template_v2.previous_version_id == template_v1.id
    assert template_v2.items.get(order=1).title == "Updated fictional item"
    assert template_v2.is_sealed is True


@pytest.mark.django_db
def test_template_version_is_unique_per_category(template_v1, hr_admin):
    with pytest.raises(IntegrityError):
        duplicate = FormTemplate(
            category=template_v1.category,
            name="Duplicate fictional template",
            version=1,
            previous_version=None,
            created_by=hr_admin,
        )
        models.Model.save(duplicate, force_insert=True, using=FormTemplate.objects.db)


@pytest.mark.django_db
def test_template_public_id_is_an_unguessable_uuid_without_employee_data(template_v1):
    assert isinstance(template_v1.public_id, uuid.UUID)
    assert template_v1.public_id.version == 4
    assert "Test Employee" not in str(template_v1.public_id)


@pytest.mark.django_db
def test_item_payloads_include_only_fields_needed_for_the_next_version(template_v1):
    payload = template_v1.item_payloads()[0]

    assert tuple(payload) == ITEM_FIELDS
    assert payload["title"] == "Test item 1"
    assert payload["weight"] == Decimal("0.60")
    assert "id" not in payload and "template_id" not in payload


@pytest.mark.django_db
def test_saved_template_versions_are_readable_but_cannot_be_deleted(template_v1):
    with pytest.raises(ValidationError, match="已保存"):
        template_v1.delete()

    assert FormTemplate.objects.get(public_id=template_v1.public_id).version == 1


@pytest.mark.django_db
def test_saved_template_versions_cannot_be_updated_in_place(template_v1):
    with pytest.raises(ValidationError, match="已保存"):
        FormTemplate.objects.filter(pk=template_v1.pk).update(
            name="Mutated fictional template"
        )

    template_v1.refresh_from_db()
    assert template_v1.name == "Test evaluation template v1"


@pytest.mark.django_db
def test_hr_edit_deletes_items_only_from_the_new_version(client, hr_admin, template_v1):
    client.force_login(hr_admin)
    edit_url = reverse("evaluations:template-edit", args=[template_v1.public_id])
    mutation_key = client.get(edit_url).context["mutation_key"]
    items = template_v1.item_payloads()
    data = formset_post_data(
        name="Edited fictional template", items=items, initial_forms=2
    )
    data["items-0-DELETE"] = "on"
    data["items-1-weight"] = "1.00000"
    data["mutation_key"] = mutation_key

    response = client.post(edit_url, data)

    assert response.status_code == 302
    template_v2 = FormTemplate.objects.get(
        category=template_v1.category, version=2
    )
    assert template_v1.items.count() == 2
    assert template_v2.items.count() == 1
    assert template_v2.items.get().title == "Test item 2"


@pytest.mark.django_db
def test_hr_can_create_the_first_template_with_a_formset(client, hr_admin):
    category = create_category("NEW", "New Template Category")
    initial_items = [
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
            "order": 2,
            "weight": "0.40",
            "score_min": 1,
            "score_max": 5,
            "excellent_description": "Excellent fictional result",
            "good_description": "Good fictional result",
            "qualified_description": "Qualified fictional result",
            "improvement_description": "Improvement fictional result",
        },
    ]
    data = formset_post_data(
        name="First fictional template", items=initial_items, initial_forms=0
    )
    data["category"] = str(category.pk)
    client.force_login(hr_admin)
    create_url = reverse("evaluations:template-create")
    data["mutation_key"] = client.get(create_url).context["mutation_key"]

    response = client.post(create_url, data)

    assert response.status_code == 302
    template = FormTemplate.objects.get(category=category)
    assert template.version == 1
    assert template.items.count() == 2


@pytest.mark.django_db
def test_template_get_requests_do_not_mutate_business_data(client, hr_admin, template_v1):
    client.force_login(hr_admin)
    before = (FormTemplate.objects.count(), template_v1.items.count())

    response = client.get(
        reverse("evaluations:template-edit", args=[template_v1.public_id])
    )

    assert response.status_code == 200
    assert before == (FormTemplate.objects.count(), template_v1.items.count())


@pytest.mark.django_db
def test_template_mutations_require_hr_role_post_and_csrf(
    client, hr_admin, template_v1, user_with_role
):
    edit_url = reverse("evaluations:template-edit", args=[template_v1.public_id])
    client.force_login(user_with_role)
    assert client.get(edit_url).status_code == 403
    assert client.post(edit_url, {}).status_code == 403

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(hr_admin)
    response = csrf_client.post(edit_url, {})

    assert response.status_code == 403
    assert FormTemplate.objects.filter(category=template_v1.category).count() == 1
