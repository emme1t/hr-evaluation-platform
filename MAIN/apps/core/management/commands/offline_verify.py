import importlib

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.offline.health import preflight_readiness
from apps.core.offline.paths import OfflinePathError, OfflinePaths


class Command(BaseCommand):
    help = "在离线启动前验证 Task 12 审计集成契约"

    def handle(self, *args, **options):
        try:
            audit_services = importlib.import_module("apps.audit.services")
            record_audit = getattr(audit_services, "record_audit")
        except (ImportError, AttributeError) as exc:
            raise CommandError("OFFLINE_AUDIT_UNAVAILABLE") from exc
        if not callable(record_audit):
            raise CommandError("OFFLINE_AUDIT_UNAVAILABLE")
        if getattr(settings, "APP_ENV", None) != "offline":
            self.stdout.write(self.style.SUCCESS("offline audit integration verified"))
            return
        try:
            paths = OfflinePaths.from_settings()
            readiness = preflight_readiness(paths)
        except OfflinePathError as exc:
            raise CommandError("OFFLINE_NOT_READY") from exc
        if readiness.status != "ready" or paths.dirty_marker.exists() or not paths.clean_marker.exists():
            raise CommandError("OFFLINE_NOT_READY")
        self.stdout.write(self.style.SUCCESS("OFFLINE_VERIFY_OK"))
