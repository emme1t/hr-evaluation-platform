from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.audit.deployment import (
    AuditDeploymentError,
    configure_audit_postgresql_acl,
)


class Command(BaseCommand):
    help = "由审计表所有者连接配置 PostgreSQL 运行角色的表和序列最小权限"

    def handle(self, *args, **options):
        try:
            evidence = configure_audit_postgresql_acl(
                connection, getattr(settings, "AUDIT_RUNTIME_DB_ROLE", "")
            )
        except AuditDeploymentError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "audit PostgreSQL ACL configuration passed "
                f"(table_oid={evidence['table_oid']}, "
                f"sequence_oid={evidence['sequence_oid']}, "
                f"runtime_role={evidence['runtime_role']})"
            )
        )
