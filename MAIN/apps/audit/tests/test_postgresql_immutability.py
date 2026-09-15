import pytest
from django.db import connection
from django.conf import settings

from apps.audit.deployment import verify_audit_postgresql_deployment
from apps.audit.services import record_audit
from tests.factories import create_employee


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="audit trigger, owner, grant, and relation OID evidence requires real PostgreSQL",
    ),
]


def test_postgresql_audit_table_relation_owner_grants_and_three_triggers_are_verified():
    evidence = verify_audit_postgresql_deployment(
        connection, settings.AUDIT_RUNTIME_DB_ROLE
    )

    assert evidence["current_user"] == evidence["runtime_role"]
    assert evidence["session_user"] == evidence["runtime_role"]
    assert evidence["owner_role"] != evidence["runtime_role"]
    assert evidence["sequence_oid"] > 0
    assert evidence["sequence_owner_role"] == evidence["owner_role"]
    assert evidence["sequence_table_oid"] == evidence["table_oid"]
    assert evidence["sequence_column_name"] == "id"
    assert {row[1] for row in evidence["triggers"]} == {evidence["table_oid"]}
    assert all(row[2] in {"O", "A"} for row in evidence["triggers"])
    assert evidence["runtime_privileges"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }
    assert evidence["runtime_sequence_privileges"] == {
        "USAGE": True,
        "SELECT": True,
        "UPDATE": False,
    }
    assert evidence["public_sequence_privileges"] == {
        "USAGE": False,
        "SELECT": False,
        "UPDATE": False,
    }


def test_runtime_role_can_append_audit_log_through_django_sequence(hr_admin):
    employee = create_employee(
        "PG-AUDIT-INSERT", "Fictional PostgreSQL Audit Insert"
    )

    event = record_audit(
        hr_admin,
        "EMPLOYEE_UPDATED",
        employee,
        {"field": "fictional"},
        idempotency_key="pg-runtime-sequence-insert",
    )

    assert event.pk is not None
