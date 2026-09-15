from django.urls import path

from . import views


app_name = "notifications"

urlpatterns = [
    path(
        "hr/projects/<uuid:project_id>/launch/",
        views.project_launch,
        name="launch",
    ),
    path(
        "hr/projects/<uuid:project_id>/notifications/",
        views.project_status,
        name="project-status",
    ),
    path(
        "hr/notifications/<uuid:outbox_id>/retry/",
        views.retry_notification,
        name="retry",
    ),
    path(
        "hr/notifications/<uuid:outbox_id>/email/",
        views.switch_to_email,
        name="switch-email",
    ),
    path(
        "auth/email/confirm/",
        views.email_magic_link,
        name="email-magic-link",
    ),
    path(
        "projects/<uuid:project_id>/tasks/",
        views.project_task_entry,
        name="project-task-entry",
    ),
]
