from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.audit.deployment import (
    AuditDeploymentError,
    verify_audit_postgresql_deployment,
)


class Command(BaseCommand):
    help = "验证 PostgreSQL 审计表角色隔离、权限与不可变触发器"

    def handle(self, *args, **options):
        try:
            evidence = verify_audit_postgresql_deployment(
                connection, getattr(settings, "AUDIT_RUNTIME_DB_ROLE", "")
            )
        except AuditDeploymentError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "audit PostgreSQL verification passed "
                f"(table_oid={evidence['table_oid']}, runtime_role={evidence['runtime_role']})"
            )
        )
