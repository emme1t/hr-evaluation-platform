from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import require_hr_role

from .forms import CategoryForm, EmployeeForm, ImportCommitForm, RelationshipForm, UploadForm
from .imports import (
    ImportCommitError,
    ImportPreviewError,
    commit_import_batch,
    has_complete_deactivation_details,
    preview_relationship_upload,
    preview_roster_upload,
)
from .models import Employee, EmployeeCategory, EvaluationRelationship, ImportBatch
from .services import (
    RosterValidationError,
    create_category_record,
    create_employee_record,
    deactivate_category,
    deactivate_employee,
    deactivate_relationship,
    update_category_record,
    update_employee_record,
    update_relationship_record,
    upsert_relationship,
)


hr_required = require_hr_role("HR_ADMIN", "HR_OPERATOR")


def _page(request, queryset):
    return Paginator(queryset, 25).get_page(request.GET.get("page"))


def _service_error(form, exc):
    form.add_error(None, str(exc))


@hr_required
@require_GET
def roster_list(request):
    employees = Employee.objects.select_related("category").order_by("employee_no")
    return render(
        request,
        "hr/roster/list.html",
        {"page_obj": _page(request, employees), "upload_form": UploadForm()},
    )


@hr_required
def employee_create(request):
    form = EmployeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            create_employee_record(actor=request.user, **form.service_values())
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:roster-list")
    return render(
        request,
        "hr/roster/edit.html",
        {"form": form, "title": "新增员工"},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
def employee_edit(request, employee_id):
    employee = get_object_or_404(Employee, pk=employee_id)
    initial = {
        "employee_no": employee.employee_no,
        "name": employee.name,
        "corporate_email": employee.corporate_email,
        "department_level_1": employee.department_level_1,
        "department_level_2": employee.department_level_2,
        "category_id": employee.category_id,
        "wecom_userid": employee.wecom_userid or "",
    }
    form = EmployeeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            update_employee_record(
                employee.pk, actor=request.user, **form.service_values()
            )
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:roster-list")
    return render(
        request,
        "hr/roster/edit.html",
        {"form": form, "title": "编辑员工", "employee": employee},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
@require_POST
def employee_deactivate(request, employee_id):
    deactivate_employee(employee_id, actor=request.user)
    return redirect("roster:roster-list")


@hr_required
@require_GET
def category_list(request):
    return render(
        request,
        "hr/categories/list.html",
        {"page_obj": _page(request, EmployeeCategory.objects.order_by("code"))},
    )


@hr_required
def category_create(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            create_category_record(actor=request.user, **form.cleaned_data)
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:category-list")
    return render(
        request,
        "hr/categories/edit.html",
        {"form": form, "title": "新增类别"},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
def category_edit(request, category_id):
    category = get_object_or_404(EmployeeCategory, pk=category_id)
    form = CategoryForm(
        request.POST or None, initial={"code": category.code, "name": category.name}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_category_record(
                category.pk, actor=request.user, **form.cleaned_data
            )
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:category-list")
    return render(
        request,
        "hr/categories/edit.html",
        {"form": form, "title": "编辑类别", "category": category},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
@require_POST
def category_deactivate(request, category_id):
    deactivate_category(category_id, actor=request.user)
    return redirect("roster:category-list")


@hr_required
@require_GET
def relationship_list(request):
    relationships = EvaluationRelationship.objects.select_related(
        "subject", "evaluator"
    ).order_by("subject__employee_no", "evaluator__employee_no")
    return render(
        request,
        "hr/relationships/list.html",
        {"page_obj": _page(request, relationships), "upload_form": UploadForm()},
    )


@hr_required
def relationship_create(request):
    form = RelationshipForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            upsert_relationship(actor=request.user, **form.cleaned_data)
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:relationship-list")
    return render(
        request,
        "hr/relationships/edit.html",
        {"form": form, "title": "新增协作关系"},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
def relationship_edit(request, relationship_id):
    relationship = get_object_or_404(
        EvaluationRelationship.objects.select_related("subject", "evaluator"),
        pk=relationship_id,
    )
    form = RelationshipForm(
        request.POST or None,
        initial={
            "subject_no": relationship.subject.employee_no,
            "evaluator_no": relationship.evaluator.employee_no,
            "relationship_type": relationship.relationship_type,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_relationship_record(
                relationship.pk, actor=request.user, **form.cleaned_data
            )
        except RosterValidationError as exc:
            _service_error(form, exc)
        else:
            return redirect("roster:relationship-list")
    return render(
        request,
        "hr/relationships/edit.html",
        {"form": form, "title": "编辑协作关系", "relationship": relationship},
        status=400 if request.method == "POST" else 200,
    )


@hr_required
@require_POST
def relationship_deactivate(request, relationship_id):
    deactivate_relationship(relationship_id, actor=request.user)
    return redirect("roster:relationship-list")


def _upload(request, preview_function):
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return HttpResponseBadRequest("；".join(form.errors.get("file", [])))
    uploaded = form.cleaned_data["file"]
    try:
        batch = preview_function(uploaded.read(), uploaded.name, request.user)
    except ImportPreviewError as exc:
        return HttpResponseBadRequest(str(exc))
    return render(request, "hr/roster/import_preview.html", _preview_context(request, batch))


@hr_required
@require_POST
def roster_upload(request):
    return _upload(request, preview_roster_upload)


@hr_required
@require_POST
def relationship_upload(request):
    return _upload(request, preview_relationship_upload)


@hr_required
@require_GET
def import_preview(request, batch_id):
    batch = get_object_or_404(ImportBatch, public_id=batch_id)
    if not has_complete_deactivation_details(batch):
        return HttpResponseBadRequest("预览停用明细校验失败，请重新预检")
    return render(request, "hr/roster/import_preview.html", _preview_context(request, batch))


def _preview_context(request, batch, commit_form=None):
    issue_page = Paginator(batch.issues.all(), 50).get_page(
        request.GET.get("issue_page")
    )
    return {
        "batch": batch,
        "commit_form": commit_form or ImportCommitForm(batch=batch),
        "issue_page": issue_page,
    }


@hr_required
@require_POST
def import_commit(request, batch_id):
    batch = get_object_or_404(ImportBatch, public_id=batch_id)
    if not has_complete_deactivation_details(batch):
        return HttpResponseBadRequest("预览停用明细校验失败，请重新预检")
    form = ImportCommitForm(request.POST, batch=batch)
    if not form.is_valid():
        return render(
            request,
            "hr/roster/import_preview.html",
            _preview_context(request, batch, form),
            status=400,
        )
    try:
        commit_import_batch(
            batch.public_id,
            mode=form.cleaned_data["mode"],
            duplicate_policy=form.cleaned_data["duplicate_policy"],
            actor=request.user,
        )
    except ImportCommitError as exc:
        form.add_error(None, str(exc))
        return render(
            request,
            "hr/roster/import_preview.html",
            _preview_context(request, batch, form),
            status=400,
        )
    destination = (
        "roster:roster-list"
        if batch.import_type == ImportBatch.Type.ROSTER
        else "roster:relationship-list"
    )
    return redirect(destination)


@hr_required
@require_GET
def employee_lookup(request):
    employee_no = request.GET.get("employee_no", "").strip()
    employee = Employee.objects.filter(
        employee_no=employee_no, is_active=True
    ).first()
    if not employee:
        return JsonResponse({"found": False})
    return JsonResponse(
        {"found": True, "employee_no": employee.employee_no, "name": employee.name}
    )
