from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import connection

from .deployment import AuditDeploymentError, verify_audit_postgresql_deployment


@register(Tags.security, deploy=True)
def audit_postgresql_deployment_check(app_configs, **kwargs):
    if getattr(settings, "APP_ENV", "local") not in {"staging", "production"}:
        return []
    try:
        verify_audit_postgresql_deployment(
            connection, getattr(settings, "AUDIT_RUNTIME_DB_ROLE", "")
        )
    except AuditDeploymentError as exc:
        return [
            Error(
                str(exc),
                hint="配置独立运行角色并执行 verify_audit_postgresql 后再发布。",
                id="audit.E001",
            )
        ]
    return []
