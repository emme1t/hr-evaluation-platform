import pytest

from apps.audit.deployment import (
    AuditDeploymentError,
    configure_audit_postgresql_acl,
)


class _QuoteOps:
    @staticmethod
    def quote_name(value):
        return f'"{value.replace(chr(34), chr(34) * 2)}"'


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.fetchone_result = None
        self.fetchall_result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.calls.append((normalized, params))
        if "SELECT table_ns.nspname" in normalized:
            self.fetchone_result = (
                "public",
                "audit_auditlog",
                4242,
                "schema_owner",
                "public",
                "audit_auditlog_id_seq",
                4343,
                "schema_owner",
                4242,
                "id",
                True,
                self.connection.current_user,
                self.connection.session_user,
            )
        elif "SELECT c.oid" in normalized:
            self.fetchone_result = (
                4242,
                "schema_owner",
                "hr_runtime",
                True,
                self.connection.current_user,
                self.connection.session_user,
                True,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            )
        elif "SELECT sequence_rel.oid" in normalized:
            self.fetchone_result = (
                4343,
                "schema_owner",
                4242,
                "id",
                True,
                True,
                False,
                False,
                False,
                False,
            )
        elif "SELECT tgname" in normalized:
            self.fetchall_result = [
                ("audit_auditlog_no_delete", 4242, "O"),
                ("audit_auditlog_no_truncate", 4242, "A"),
                ("audit_auditlog_no_update", 4242, "O"),
            ]

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result


class _FakePostgreSQLConnection:
    vendor = "postgresql"
    ops = _QuoteOps()

    def __init__(self, *, current_user="schema_owner", session_user="schema_owner"):
        self.current_user = current_user
        self.session_user = session_user
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)


def test_owner_run_acl_service_applies_table_and_sequence_least_privilege_then_verifies():
    connection = _FakePostgreSQLConnection()

    evidence = configure_audit_postgresql_acl(connection, "hr_runtime")

    mutation_sql = [
        sql for sql, _ in connection.calls if sql.startswith(("GRANT", "REVOKE"))
    ]
    assert mutation_sql == [
        'GRANT SELECT, INSERT ON TABLE "public"."audit_auditlog" TO "hr_runtime"',
        'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE "public"."audit_auditlog" FROM "hr_runtime"',
        'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE "public"."audit_auditlog" FROM PUBLIC',
        'GRANT USAGE, SELECT ON SEQUENCE "public"."audit_auditlog_id_seq" TO "hr_runtime"',
        'REVOKE UPDATE ON SEQUENCE "public"."audit_auditlog_id_seq" FROM "hr_runtime"',
        'REVOKE USAGE, SELECT, UPDATE ON SEQUENCE "public"."audit_auditlog_id_seq" FROM PUBLIC',
    ]
    assert evidence["sequence_oid"] == 4343
    assert evidence["current_user"] == "schema_owner"


def test_acl_service_rejects_non_owner_connection_before_any_grant():
    connection = _FakePostgreSQLConnection(
        current_user="deployment_helper", session_user="deployment_helper"
    )

    with pytest.raises(AuditDeploymentError, match="所有者"):
        configure_audit_postgresql_acl(connection, "hr_runtime")

    assert not any(
        sql.startswith(("GRANT", "REVOKE")) for sql, _ in connection.calls
    )
