from django.urls import path

from . import views


app_name = "reporting"

urlpatterns = [
    path("templates/", views.template_list, name="template-list"),
    path("templates/upload/", views.template_upload, name="template-upload"),
    path(
        "projects/<uuid:project_id>/summary/",
        views.summary_download,
        name="summary-download",
    ),
    path(
        "projects/<uuid:project_id>/raw/",
        views.raw_download,
        name="raw-download",
    ),
    path(
        "projects/<uuid:project_id>/subjects/<uuid:subject_id>/raw/",
        views.raw_download,
        name="raw-subject-download",
    ),
    path(
        "projects/<uuid:project_id>/issues/",
        views.issues_download,
        name="issues-download",
    ),
]
