from django.urls import path

from . import views


app_name = "roster"

urlpatterns = [
    path("roster/", views.roster_list, name="roster-list"),
    path("roster/new/", views.employee_create, name="employee-create"),
    path("roster/<int:employee_id>/edit/", views.employee_edit, name="employee-edit"),
    path(
        "roster/<int:employee_id>/deactivate/",
        views.employee_deactivate,
        name="employee-deactivate",
    ),
    path("roster/upload/", views.roster_upload, name="roster-upload"),
    path("categories/", views.category_list, name="category-list"),
    path("categories/new/", views.category_create, name="category-create"),
    path(
        "categories/<int:category_id>/edit/", views.category_edit, name="category-edit"
    ),
    path(
        "categories/<int:category_id>/deactivate/",
        views.category_deactivate,
        name="category-deactivate",
    ),
    path("relationships/", views.relationship_list, name="relationship-list"),
    path(
        "relationships/new/", views.relationship_create, name="relationship-create"
    ),
    path(
        "relationships/<int:relationship_id>/edit/",
        views.relationship_edit,
        name="relationship-edit",
    ),
    path(
        "relationships/<int:relationship_id>/deactivate/",
        views.relationship_deactivate,
        name="relationship-deactivate",
    ),
    path(
        "relationships/upload/", views.relationship_upload, name="relationship-upload"
    ),
    path("imports/<uuid:batch_id>/", views.import_preview, name="import-preview"),
    path("imports/<uuid:batch_id>/commit/", views.import_commit, name="import-commit"),
    path("employees/search/", views.employee_lookup, name="employee-lookup"),
]
