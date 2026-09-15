from django.urls import path

from . import views


app_name = "audit"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("audit/", views.audit_list, name="list"),
    path("audit/export/", views.audit_export, name="export"),
]
