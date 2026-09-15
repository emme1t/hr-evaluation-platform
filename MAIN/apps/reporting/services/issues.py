from io import BytesIO

from openpyxl import Workbook

from apps.evaluations.models import EvaluationProject

from .common import (
    OutputBoundaryError,
    ReportingSnapshotUnavailable,
    frozen_project_name,
    safe_cell_text,
)
from .summary import TemplateStructureError


MAX_ISSUE_ROWS = 10_000
REMEDIATION = {
    "TASK_NOT_SUBMITTED": "联系评价人完成提交；截止后确认是否保持数据不完整",
    "INCOMPLETE_RESULT": "核对缺失关系组及正式提交，不得重归一化剩余权重",
}


def _frozen_employee_no(project_subject):
    snapshot = project_subject.reporting_snapshot
    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {"employee_no", "corporate_email", "department", "manager_name"}
        or not isinstance(snapshot.get("employee_no"), str)
        or not snapshot["employee_no"]
    ):
        raise TemplateStructureError(
            "项目导出冻结员工编号无效", "REPORT_DATA_INVALID"
        )
    return snapshot["employee_no"]


def _issue_rows(project, project_name):
    project_subjects = {
        row.pk: row for row in project.subjects.select_related("subject").order_by("public_id")
    }
    rows = []
    for task in project.tasks.order_by("project_subject__public_id", "public_id"):
        project_subject = project_subjects.get(task.project_subject_id)
        relationship = next(
            (
                item
                for item in project_subject.relationship_snapshot
                if item.get("snapshot_item_id")
                == str(task.relationship_snapshot_item_id)
            ),
            None,
        ) if project_subject else None
        if (
            project_subject is None
            or task.project_id != project.pk
            or task.subject_id != project_subject.subject_id
            or relationship is None
            or relationship.get("relationship_type") != task.relationship_type
        ):
            raise TemplateStructureError(
                "项目任务与冻结关系不一致", "REPORT_DATA_INVALID"
            )
        if task.status != task.Status.SUBMITTED:
            rows.append(
                (
                    "TASK_NOT_SUBMITTED",
                    project_name,
                    _frozen_employee_no(project_subject),
                    relationship["relationship_type"],
                    task.status,
                    REMEDIATION["TASK_NOT_SUBMITTED"],
                )
            )
    for result in project.aggregate_results.select_related("project_subject").order_by(
        "project_subject__public_id", "public_id"
    ):
        project_subject = project_subjects.get(result.project_subject_id)
        if (
            project_subject is None
            or result.project_id != project.pk
            or result.subject_id != project_subject.subject_id
            or str(result.frozen_subject_public_id)
            != project_subject.subject_snapshot.get("public_id")
        ):
            raise TemplateStructureError(
                "汇总结果与冻结项目不一致", "REPORT_DATA_INVALID"
            )
        if result.status == result.Status.INCOMPLETE:
            for group in result.missing_groups:
                rows.append(
                    (
                        "INCOMPLETE_RESULT",
                        project_name,
                        _frozen_employee_no(project_subject),
                        group,
                        "数据不完整",
                        REMEDIATION["INCOMPLETE_RESULT"],
                    )
                )
    if len(rows) > MAX_ISSUE_ROWS:
        raise TemplateStructureError("异常说明超过资源边界", "ISSUE_OUTPUT_LIMIT")
    return rows


def export_issue_workbook(project):
    project = EvaluationProject.objects.get(pk=project.pk)
    if project.status == EvaluationProject.Status.DRAFT or project.prepared_at is None:
        raise TemplateStructureError("项目尚未准备，不能导出异常说明")
    try:
        project_name = frozen_project_name(project)
    except ReportingSnapshotUnavailable as exc:
        raise TemplateStructureError(
            str(exc), "REPORTING_SNAPSHOT_UNAVAILABLE"
        ) from exc
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "异常说明"
    worksheet.append(
        (
            "错误码",
            "项目",
            "被评价人员工编号",
            "关系类型",
            "任务状态",
            "建议处理方式",
        )
    )
    try:
        for row in _issue_rows(project, project_name):
            worksheet.append(tuple(safe_cell_text(value) for value in row))
    except OutputBoundaryError as exc:
        raise TemplateStructureError(str(exc), "ISSUE_OUTPUT_LIMIT") from exc
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
