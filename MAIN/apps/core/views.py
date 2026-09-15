import json

from django.conf import settings
from django.http import JsonResponse


def health(request):
    if getattr(settings, "APP_ENV", None) == "offline":
        from apps.core.offline.health import preflight_readiness
        from apps.core.offline.paths import OfflinePathError, OfflinePaths

        try:
            paths = OfflinePaths.from_settings()
            readiness = preflight_readiness(paths)
            clean = json.loads(paths.clean_marker.read_text(encoding="utf-8"))
            generation = clean.get("generation")
        except (OSError, ValueError, json.JSONDecodeError, OfflinePathError):
            readiness = None
            generation = None
        if readiness is None or readiness.status != "ready" or not isinstance(generation, str):
            return JsonResponse(
                {"status": "not_ready", "code": "OFFLINE_NOT_READY", "mode": "offline"},
                status=503,
            )
        return JsonResponse(
            {
                "status": "ok",
                "database": "ok",
                "mode": "offline",
                "generation": generation,
            }
        )
    return JsonResponse({"status": "ok", "database": "ok"})
