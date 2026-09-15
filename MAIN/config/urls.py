from django.urls import include, path

from apps.core.views import health

urlpatterns = [
    path("health/", health, name="health"),
    path("auth/", include("apps.accounts.urls")),
    path("", include("apps.notifications.urls")),
    path("hr/", include("apps.audit.urls")),
    path("hr/", include("apps.roster.urls")),
    path("", include("apps.evaluations.urls")),
    path("hr/reporting/", include("apps.reporting.urls")),
]
