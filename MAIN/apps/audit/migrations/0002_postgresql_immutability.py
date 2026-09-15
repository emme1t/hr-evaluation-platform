from django.db import migrations


CREATE_POSTGRESQL_GUARDS = """
CREATE OR REPLACE FUNCTION audit_auditlog_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditlog is append-only';
END;
$$;

CREATE TRIGGER audit_auditlog_no_update
BEFORE UPDATE ON audit_auditlog
FOR EACH ROW EXECUTE FUNCTION audit_auditlog_reject_mutation();

CREATE TRIGGER audit_auditlog_no_delete
BEFORE DELETE ON audit_auditlog
FOR EACH ROW EXECUTE FUNCTION audit_auditlog_reject_mutation();

CREATE TRIGGER audit_auditlog_no_truncate
BEFORE TRUNCATE ON audit_auditlog
FOR EACH STATEMENT EXECUTE FUNCTION audit_auditlog_reject_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_auditlog FROM PUBLIC;
"""


DROP_POSTGRESQL_GUARDS = """
DROP TRIGGER IF EXISTS audit_auditlog_no_update ON audit_auditlog;
DROP TRIGGER IF EXISTS audit_auditlog_no_delete ON audit_auditlog;
DROP TRIGGER IF EXISTS audit_auditlog_no_truncate ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_auditlog_reject_mutation();
"""


def create_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_POSTGRESQL_GUARDS)


def drop_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_POSTGRESQL_GUARDS)


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunPython(create_guards, drop_guards)]
