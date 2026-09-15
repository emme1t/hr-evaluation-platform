from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


REVERSE_ERROR = (
    "audit runtime ACL downgrade is unsupported; restore a verified database "
    "backup instead"
)


def preserve_runtime_acl_boundary(apps, schema_editor):
    pass


def block_runtime_acl_reverse(apps, schema_editor):
    raise IrreversibleError(REVERSE_ERROR)


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_runtime_role_and_manager_contract")]

    operations = [
        migrations.RunPython(
            preserve_runtime_acl_boundary,
            block_runtime_acl_reverse,
        )
    ]
