from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from uuid import UUID, uuid4

from apps.core.permissions import require_hr_role
from apps.audit.models import AuditLog

from ..forms.templates import TemplateCreateForm, TemplateItemFormSet, TemplateVersionForm
from ..models.templates import FormTemplate, ITEM_PAYLOAD_FIELDS
from ..services.templates import (
    TemplateValidationError,
    create_initial_template,
    create_template_version,
)


hr_required = require_hr_role("HR_ADMIN", "HR_OPERATOR")


def _stable_mutation_key(request):
    if request.method != "POST":
        return str(uuid4())
    value = request.POST.get("mutation_key", "")
    try:
        if value and str(UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _item_payloads(formset):
    return [
        {field: form.cleaned_data[field] for field in ITEM_PAYLOAD_FIELDS}
        for form in formset.forms
        if form.cleaned_data and not form.cleaned_data.get("DELETE")
    ]


@hr_required
@require_GET
def template_list(request):
    templates = FormTemplate.objects.select_related(
        "category", "created_by", "previous_version"
    ).order_by("category__code", "-version")
    return render(request, "hr/templates/list.html", {"templates": templates})


@hr_required
@require_http_methods(["GET", "POST"])
def template_create(request):
    mutation_key = _stable_mutation_key(request)
    mutation_control_valid = mutation_key is not None
    if request.method == "POST" and mutation_control_valid:
        audit_key = f"template-create:{request.user.public_id}:{mutation_key}"
        if AuditLog.objects.filter(
            actor=request.user,
            action="TEMPLATE_CREATED",
            idempotency_key=audit_key,
        ).exists():
            return redirect("evaluations:template-list")
    form = TemplateCreateForm(request.POST or None)
    formset = TemplateItemFormSet(request.POST or None, prefix="items")
    if request.method == "POST" and not mutation_control_valid:
        form.add_error(None, "操作校验无效，请刷新页面后重试")
    elif request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            create_initial_template(
                category=form.cleaned_data["category"],
                name=form.cleaned_data["name"],
                items=_item_payloads(formset),
                actor=request.user,
                mutation_key=mutation_key,
            )
        except TemplateValidationError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("evaluations:template-list")
    return render(
        request,
        "hr/templates/edit.html",
        {
            "form": form,
            "formset": formset,
            "title": "新建评价表模板",
            "source": None,
            "mutation_key": mutation_key or "",
        },
        status=400 if request.method == "POST" else 200,
    )


@hr_required
@require_http_methods(["GET", "POST"])
def template_edit(request, template_id):
    mutation_key = _stable_mutation_key(request)
    mutation_control_valid = mutation_key is not None
    source = get_object_or_404(
        FormTemplate.objects.select_related("category"), public_id=template_id
    )
    form = TemplateVersionForm(request.POST or None, initial={"name": source.name})
    formset = TemplateItemFormSet(
        request.POST or None, initial=source.item_payloads(), prefix="items"
    )
    if request.method == "POST" and mutation_control_valid:
        audit_key = f"template-version:{request.user.public_id}:{mutation_key}"
        if AuditLog.objects.filter(
            actor=request.user,
            action="TEMPLATE_VERSION_CREATED",
            idempotency_key=audit_key,
        ).exists():
            return redirect("evaluations:template-list")
    if request.method == "POST" and not mutation_control_valid:
        form.add_error(None, "操作校验无效，请刷新页面后重试")
    elif request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            create_template_version(
                source,
                changes={
                    "name": form.cleaned_data["name"],
                    "items": _item_payloads(formset),
                },
                actor=request.user,
                mutation_key=mutation_key,
            )
        except TemplateValidationError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("evaluations:template-list")
    return render(
        request,
        "hr/templates/edit.html",
        {
            "form": form,
            "formset": formset,
            "title": f"编辑评价表模板：{source.name}（保存为新版本）",
            "source": source,
            "mutation_key": mutation_key or "",
        },
        status=400 if request.method == "POST" else 200,
    )
