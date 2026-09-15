from django import forms
from django.forms import formset_factory

from apps.roster.models import EmployeeCategory


class TemplateCreateForm(forms.Form):
    category = forms.ModelChoiceField(
        label="员工类别", queryset=EmployeeCategory.objects.none()
    )
    name = forms.CharField(label="评价表名称", max_length=160)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = EmployeeCategory.objects.filter(
            is_active=True
        ).order_by("code")


class TemplateVersionForm(forms.Form):
    name = forms.CharField(label="评价表名称", max_length=160)


class TemplateItemForm(forms.Form):
    group = forms.CharField(label="评价组", max_length=80)
    title = forms.CharField(label="评价项", max_length=200)
    order = forms.IntegerField(label="序号")
    weight = forms.DecimalField(label="权重", max_digits=6, decimal_places=5)
    score_min = forms.IntegerField(label="最低分")
    score_max = forms.IntegerField(label="最高分")
    excellent_description = forms.CharField(label="优秀描述", widget=forms.Textarea)
    good_description = forms.CharField(label="良好描述", widget=forms.Textarea)
    qualified_description = forms.CharField(label="合格描述", widget=forms.Textarea)
    improvement_description = forms.CharField(label="待改进描述", widget=forms.Textarea)


TemplateItemFormSet = formset_factory(TemplateItemForm, extra=1, can_delete=True)
