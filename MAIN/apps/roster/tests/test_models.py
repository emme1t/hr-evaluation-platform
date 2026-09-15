import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.roster.models import Employee, EmployeeCategory


def create_category(**overrides):
    defaults = {"code": "HR", "name": "Human Resources"}
    defaults.update(overrides)
    return EmployeeCategory.objects.create(**defaults)


def create_employee(category, **overrides):
    defaults = {
        "employee_no": "E001",
        "name": "Test Employee",
        "corporate_email": "employee@example.test",
        "department_level_1": "Test Center",
        "department_level_2": "Test Team",
        "category": category,
        "wecom_userid": "wx_e001",
    }
    defaults.update(overrides)
    return Employee.objects.create(**defaults)


@pytest.mark.django_db
def test_employee_category_code_is_unique():
    create_category()

    with pytest.raises(IntegrityError):
        create_category(code="HR", name="Operations")


@pytest.mark.django_db
def test_employee_category_name_is_unique():
    create_category()

    with pytest.raises(IntegrityError):
        create_category(code="OPS", name="Human Resources")


@pytest.mark.django_db
def test_employee_category_public_id_is_unique():
    public_id = uuid.uuid4()
    create_category(public_id=public_id)

    with pytest.raises(IntegrityError):
        create_category(code="OPS", name="Operations", public_id=public_id)


@pytest.mark.django_db
def test_employee_number_is_unique():
    category = create_category()
    create_employee(category)

    with pytest.raises(IntegrityError):
        create_employee(
            category,
            employee_no="E001",
            corporate_email="other@example.test",
            wecom_userid="wx_e002",
        )


@pytest.mark.django_db
def test_employee_corporate_email_is_unique():
    category = create_category()
    create_employee(category)

    with pytest.raises(IntegrityError):
        create_employee(
            category,
            employee_no="E002",
            corporate_email="employee@example.test",
            wecom_userid="wx_e002",
        )


@pytest.mark.django_db
def test_employee_wecom_userid_is_unique_when_present():
    category = create_category()
    create_employee(category)

    with pytest.raises(IntegrityError):
        create_employee(
            category,
            employee_no="E002",
            corporate_email="other@example.test",
            wecom_userid="wx_e001",
        )


@pytest.mark.django_db
def test_employee_public_id_is_unique():
    category = create_category()
    public_id = uuid.uuid4()
    create_employee(category, public_id=public_id)

    with pytest.raises(IntegrityError):
        create_employee(
            category,
            employee_no="E002",
            corporate_email="other@example.test",
            wecom_userid="wx_e002",
            public_id=public_id,
        )


@pytest.mark.django_db
def test_employee_user_is_one_to_one():
    category = create_category()
    user = get_user_model().objects.create_user("employee@example.test")
    create_employee(category, user=user)

    with pytest.raises(IntegrityError):
        create_employee(
            category,
            employee_no="E002",
            corporate_email="other@example.test",
            wecom_userid="wx_e002",
            user=user,
        )
