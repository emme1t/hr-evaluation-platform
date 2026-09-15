from django import forms
from django.db import transaction

from apps.roster.models import Employee

from ..models.projects import EvaluationProject, ProjectSubject
from ..models.templates import FormTemplate
from ..services.projects import ProjectValidationError, normalize_project_rules


class ProjectForm(forms.ModelForm):
    subjects = forms.ModelMultipleChoiceField(
        label="被评价人",
        queryset=Employee.objects.none(),
    )
    manager_weight = forms.DecimalField(
        label="上级权重", min_value=None, max_digits=8, decimal_places=5, initial="0.50"
    )
    same_department_weight = forms.DecimalField(
        label="同部门权重",
        min_value=None,
        max_digits=8,
        decimal_places=5,
        initial="0.30",
    )
    cross_department_weight = forms.DecimalField(
        label="跨部门权重",
        min_value=None,
        max_digits=8,
        decimal_places=5,
        initial="0.20",
    )

    class Meta:
        model = EvaluationProject
        fields = ("name", "deadline")
        labels = {"name": "项目名称", "deadline": "截止时间"}
        widgets = {"deadline": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subjects"].queryset = Employee.objects.filter(
            is_active=True, category__is_active=True
        ).select_related("category").order_by("department_level_1", "name")
        if self.instance.pk:
            self.fields["subjects"].initial = self.instance.subjects.values_list(
                "subject_id", flat=True
            )
            for group, field_name in (
                ("manager", "manager_weight"),
                ("same_department", "same_department_weight"),
                ("cross_department", "cross_department_weight"),
            ):
                if group in self.instance.rule_snapshot:
                    self.fields[field_name].initial = self.instance.rule_snapshot[group]

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.status != EvaluationProject.Status.DRAFT:
            raise forms.ValidationError("只有草稿项目可以修改")
        if all(
            field in cleaned
            for field in (
                "manager_weight",
                "same_department_weight",
                "cross_department_weight",
            )
        ):
            try:
                normalized = normalize_project_rules(
                    {
                        "manager": cleaned["manager_weight"],
                        "same_department": cleaned["same_department_weight"],
                        "cross_department": cleaned["cross_department_weight"],
                    }
                )
            except ProjectValidationError as exc:
                raise forms.ValidationError(str(exc)) from exc
            self._draft_rules = {
                group: normalized[group]
                for group in ("manager", "same_department", "cross_department")
            }
        subjects = cleaned.get("subjects")
        if subjects:
            self._templates_by_category = self._resolve_templates(subjects)
        return cleaned

    def _resolve_templates(self, subjects):
        templates = {}
        for category_id in {subject.category_id for subject in subjects}:
            template = (
                FormTemplate.objects.filter(
                    category_id=category_id, is_active=True, is_sealed=True
                )
                .order_by("-version")
                .first()
            )
            if template is None:
                raise forms.ValidationError("被评价人员工类别缺少已密封的有效模板")
            templates[category_id] = template
        return templates

    def save(self, commit=True):
        project = super().save(commit=False)
        project.rule_snapshot = self._draft_rules
        if not commit:
            return project
        with transaction.atomic():
            project.save()
            selected_ids = {subject.pk for subject in self.cleaned_data["subjects"]}
            project.subjects.exclude(subject_id__in=selected_ids).delete()
            existing_ids = set(
                project.subjects.filter(subject_id__in=selected_ids).values_list(
                    "subject_id", flat=True
                )
            )
            ProjectSubject.objects.bulk_create(
                [
                    ProjectSubject(
                        project=project,
                        subject=subject,
                        template=self._templates_by_category[subject.category_id],
                    )
                    for subject in self.cleaned_data["subjects"]
                    if subject.pk not in existing_ids
                ]
            )
        return project


class DeadlineExtensionForm(forms.Form):
    deadline = forms.DateTimeField(
        label="新截止时间",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
