import pytest

from apps.audit.deployment import (
    AuditDeploymentError,
    validate_audit_postgresql_evidence,
)


def _valid_evidence():
    return {
        "table_oid": 4242,
        "owner_role": "schema_owner",
        "runtime_role": "hr_runtime",
        "runtime_role_exists": True,
        "current_user": "hr_runtime",
        "session_user": "hr_runtime",
        "sequence_oid": 4343,
        "sequence_owner_role": "schema_owner",
        "sequence_table_oid": 4242,
        "sequence_column_name": "id",
        "runtime_privileges": {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        },
        "runtime_sequence_privileges": {
            "USAGE": True,
            "SELECT": True,
            "UPDATE": False,
        },
        "public_mutation_privileges": {
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        },
        "public_sequence_privileges": {
            "USAGE": False,
            "SELECT": False,
            "UPDATE": False,
        },
        "triggers": [
            ("audit_auditlog_no_delete", 4242, "O"),
            ("audit_auditlog_no_truncate", 4242, "O"),
            ("audit_auditlog_no_update", 4242, "O"),
        ],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(runtime_role_exists=False),
        lambda row: row.update(runtime_role="schema_owner"),
        lambda row: row.update(current_user="schema_owner"),
        lambda row: row.update(session_user="schema_owner"),
        lambda row: row.update(sequence_oid=None),
        lambda row: row.update(sequence_owner_role="other_owner"),
        lambda row: row.update(sequence_table_oid=9999),
        lambda row: row.update(sequence_column_name="other_id"),
        lambda row: row["runtime_privileges"].update(UPDATE=True),
        lambda row: row["runtime_privileges"].update(INSERT=False),
        lambda row: row["runtime_sequence_privileges"].update(USAGE=False),
        lambda row: row["runtime_sequence_privileges"].update(SELECT=False),
        lambda row: row["runtime_sequence_privileges"].update(UPDATE=True),
        lambda row: row["public_mutation_privileges"].update(DELETE=True),
        lambda row: row["public_sequence_privileges"].update(USAGE=True),
        lambda row: row["triggers"].pop(),
        lambda row: row["triggers"].__setitem__(0, (row["triggers"][0][0], 9999, "O")),
        lambda row: row["triggers"].__setitem__(0, (row["triggers"][0][0], 4242, "D")),
        lambda row: row["triggers"].__setitem__(0, (row["triggers"][0][0], 4242, "R")),
    ],
)
def test_deployment_evidence_fails_closed_for_role_grant_oid_or_trigger_gap(mutate):
    evidence = _valid_evidence()
    mutate(evidence)

    with pytest.raises(AuditDeploymentError):
        validate_audit_postgresql_evidence(evidence)


def test_deployment_evidence_accepts_runtime_identity_table_sequence_and_origin_guards():
    assert validate_audit_postgresql_evidence(_valid_evidence())["table_oid"] == 4242


def test_owner_run_acl_verification_can_defer_only_connection_identity_gate():
    evidence = _valid_evidence()
    evidence.update(current_user="schema_owner", session_user="schema_owner")

    assert validate_audit_postgresql_evidence(
        evidence, require_connection_identity=False
    )["sequence_oid"] == 4343
