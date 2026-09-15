from django import forms


class SummaryTemplateUploadForm(forms.Form):
    file = forms.FileField(label="汇总模板（.xlsx）")
