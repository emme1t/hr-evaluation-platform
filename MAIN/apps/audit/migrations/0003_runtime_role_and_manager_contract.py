from django.conf import settings
from django.db import migrations


def configure_runtime_role(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    runtime_role = getattr(settings, "AUDIT_RUNTIME_DB_ROLE", "").strip()
    if not runtime_role:
        raise RuntimeError("AUDIT_RUNTIME_DB_ROLE is required for PostgreSQL")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT runtime.rolname, pg_get_userbyid(c.relowner)
            FROM pg_class c
            LEFT JOIN pg_roles runtime ON runtime.rolname = %s
            WHERE c.oid = to_regclass('audit_auditlog')
            """,
            [runtime_role],
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("configured audit runtime role does not exist")
    if row[0] == row[1]:
        raise RuntimeError("audit runtime role must differ from table owner")
    quoted_role = schema_editor.connection.ops.quote_name(runtime_role)
    schema_editor.execute(
        f"GRANT SELECT, INSERT ON TABLE audit_auditlog TO {quoted_role}"
    )
    schema_editor.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_auditlog FROM {quoted_role}"
    )


def remove_runtime_role_grants(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    runtime_role = getattr(settings, "AUDIT_RUNTIME_DB_ROLE", "").strip()
    if not runtime_role:
        return
    quoted_role = schema_editor.connection.ops.quote_name(runtime_role)
    schema_editor.execute(
        f"REVOKE SELECT, INSERT ON TABLE audit_auditlog FROM {quoted_role}"
    )


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_postgresql_immutability")]

    operations = [
        migrations.AlterModelOptions(
            name="auditlog",
            options={
                "base_manager_name": "objects",
                "default_manager_name": "objects",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.RunPython(configure_runtime_role, remove_runtime_role_grants),
    ]
