from django.urls import path

from .views import evaluator, projects, templates


app_name = "evaluations"

urlpatterns = [
    path("tasks/", evaluator.task_list, name="task_list"),
    path(
        "tasks/<uuid:public_id>/",
        evaluator.task_detail,
        name="task_detail",
    ),
    path(
        "tasks/<uuid:public_id>/draft/",
        evaluator.task_draft,
        name="task-draft",
    ),
    path(
        "tasks/<uuid:public_id>/submit/",
        evaluator.task_submit,
        name="task-submit",
    ),
    path("hr/projects/new/", projects.project_create, name="project-create"),
    path("hr/projects/", projects.project_list, name="project-list"),
    path(
        "hr/projects/<uuid:project_id>/preview/",
        projects.project_preview,
        name="project-preview",
    ),
    path(
        "hr/projects/<uuid:project_id>/prepare/",
        projects.project_prepare,
        name="project-prepare",
    ),
    path(
        "hr/projects/<uuid:project_id>/",
        projects.project_detail,
        name="project-detail",
    ),
    path(
        "hr/projects/<uuid:project_id>/extend-deadline/",
        projects.project_extend_deadline,
        name="project-extend-deadline",
    ),
    path(
        "hr/projects/<uuid:project_id>/close/",
        projects.project_close,
        name="project-close",
    ),
    path("hr/templates/", templates.template_list, name="template-list"),
    path("hr/templates/new/", templates.template_create, name="template-create"),
    path(
        "hr/templates/<uuid:template_id>/edit/",
        templates.template_edit,
        name="template-edit",
    ),
]
