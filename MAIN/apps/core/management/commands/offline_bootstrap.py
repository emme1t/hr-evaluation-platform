import json

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core.offline.health import preflight_readiness
from apps.core.offline.paths import OfflinePathError, OfflinePaths
from apps.core.offline.recovery import recover_dirty_start


class Command(BaseCommand):
    help = "初始化私有离线数据目录并验证 SQLite 就绪状态"

    def handle(self, *args, **options):
        try:
            paths = OfflinePaths.from_settings()
            paths.ensure_layout()
            paths.ensure_secret()
        except (OSError, OfflinePathError) as exc:
            raise CommandError("OFFLINE_BOOTSTRAP_LAYOUT_FAILED") from exc

        if paths.database.exists():
            recovered = recover_dirty_start(paths)
            if recovered.status not in {"clean", "recovered"}:
                raise CommandError(
                    f"{recovered.code}: restore from a verified offline backup"
                )
        else:
            paths.dirty_marker.write_text(json.dumps({"status": "bootstrap"}), encoding="utf-8")
            try:
                call_command("migrate", interactive=False, verbosity=options.get("verbosity", 1))
            except Exception as exc:
                raise CommandError("OFFLINE_BOOTSTRAP_MIGRATION_FAILED") from exc
            readiness = preflight_readiness(paths)
            if readiness.status != "ready":
                raise CommandError(
                    f"{readiness.code}: restore from a verified offline backup"
                )
            recovered = recover_dirty_start(paths)
            if recovered.status != "recovered":
                raise CommandError(
                    f"{recovered.code}: restore from a verified offline backup"
                )

        readiness = preflight_readiness(paths)
        if readiness.status != "ready":
            raise CommandError(f"{readiness.code}: restore from a verified offline backup")
        self.stdout.write(self.style.SUCCESS("OFFLINE_BOOTSTRAP_OK"))
