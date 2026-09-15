import pytest
from django.contrib.auth.models import Group

from apps.roster.services import ensure_default_groups


@pytest.mark.django_db
def test_migration_initializes_default_groups_without_a_service_call():
    assert set(Group.objects.values_list("name", flat=True)) >= {
        "HR_ADMIN",
        "HR_OPERATOR",
        "EVALUATOR",
    }


@pytest.mark.django_db
def test_default_hr_groups_are_created_idempotently():
    ensure_default_groups()
    ensure_default_groups()

    assert set(Group.objects.values_list("name", flat=True)) >= {
        "HR_ADMIN",
        "HR_OPERATOR",
        "EVALUATOR",
    }
    assert Group.objects.filter(name__in=["HR_ADMIN", "HR_OPERATOR", "EVALUATOR"]).count() == 3
