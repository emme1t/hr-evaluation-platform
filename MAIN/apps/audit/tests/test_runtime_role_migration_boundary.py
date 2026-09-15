from importlib import import_module

import pytest
from django.db.migrations.exceptions import IrreversibleError
from django.test import override_settings


def test_runtime_acl_migration_boundary_reverse_is_stable_and_irreversible():
    migration = import_module(
        "apps.audit.migrations.0004_runtime_acl_reverse_boundary"
    )

    with override_settings(AUDIT_RUNTIME_DB_ROLE="mutable_role_after_deploy"):
        with pytest.raises(IrreversibleError) as raised:
            migration.block_runtime_acl_reverse(None, None)

    assert str(raised.value) == (
        "audit runtime ACL downgrade is unsupported; restore a verified database "
        "backup instead"
    )
    assert migration.Migration.dependencies == [
        ("audit", "0003_runtime_role_and_manager_contract")
    ]
