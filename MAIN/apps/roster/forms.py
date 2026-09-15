from pathlib import Path

from django import forms

from .imports import MAX_UPLOAD_BYTES, has_complete_deactivation_details
from .models import EmployeeCategory, EvaluationRelationship


class EmployeeForm(forms.Form):
    employee_no = forms.CharField(label="员工编号", max_length=40)
    name = forms.CharField(label="姓名", max_length=100)
    corporate_email = forms.EmailField(label="企业邮箱", max_length=254)
    department_level_1 = forms.CharField(label="一级部门", max_length=120)
    department_level_2 = forms.CharField(label="二级部门", max_length=120)
    category_id = forms.ModelChoiceField(
        label="员工类别", queryset=EmployeeCategory.objects.none()
    )
    wecom_userid = forms.CharField(
        label="企业微信 UserId", max_length=128, required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category_id"].queryset = EmployeeCategory.objects.filter(
            is_active=True
        ).order_by("code")

    def service_values(self):
        values = self.cleaned_data.copy()
        values["category_id"] = values["category_id"].pk
        return values


class CategoryForm(forms.Form):
    code = forms.CharField(label="类别编码", max_length=40)
    name = forms.CharField(label="类别名称", max_length=100)


class RelationshipForm(forms.Form):
    subject_no = forms.CharField(label="被评价人工号", max_length=40)
    evaluator_no = forms.CharField(label="评价人工号", max_length=40)
    relationship_type = forms.ChoiceField(
        label="关系类型", choices=EvaluationRelationship.Type.choices
    )


class UploadForm(forms.Form):
    file = forms.FileField(label="导入文件")

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if Path(uploaded.name).suffix.lower() not in {".xlsx", ".csv"}:
            raise forms.ValidationError("仅支持 .xlsx 或 .csv 文件")
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("上传文件不能超过 10MB")
        return uploaded


class ImportCommitForm(forms.Form):
    mode = forms.ChoiceField(
        label="导入模式", choices=(("append", "追加"), ("replace", "覆盖"))
    )
    duplicate_policy = forms.ChoiceField(
        label="重复记录", choices=(("skip", "跳过"), ("update", "更新"))
    )
    confirm_replace = forms.BooleanField(label="确认停用预览所列记录", required=False)

    def __init__(self, *args, batch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch = batch
        if batch is not None:
            self.fields["confirm_replace"].label = (
                f"确认停用上方完整列出的 {batch.deactivate_count} 条记录"
            )
            if not self._has_complete_deactivation_details():
                self.fields["confirm_replace"].disabled = True

    def _has_complete_deactivation_details(self):
        return self.batch is None or has_complete_deactivation_details(self.batch)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") == "replace":
            if not self._has_complete_deactivation_details():
                self.add_error(
                    "confirm_replace", "预览缺少完整停用明细，请重新预检"
                )
            elif not cleaned.get("confirm_replace"):
                self.add_error("confirm_replace", "覆盖模式需要二次确认")
        return cleaned
