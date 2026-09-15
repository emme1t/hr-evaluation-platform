import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from xml.etree import ElementTree

from openpyxl import Workbook

from apps.evaluations.models import (
    EvaluationProject,
    ProjectSubject,
    Submission,
)

from .common import (
    OutputBoundaryError,
    ReportingSnapshotUnavailable,
    ensure_safe_member_path,
    frozen_project_name,
    safe_cell_text,
    safe_filename_component,
)
from .summary import require_hr_admin


MAX_OUTPUT_MEMBERS = 501
MAX_OUTPUT_MEMBER_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_COMPRESSION_RATIO = 200
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_DOCUMENT_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
CORE_PROPERTIES_MODIFIED = (
    "{http://purl.org/dc/terms/}modified"
)


class RawExportError(ValueError):
    def __init__(self, message, code="RAW_EXPORT_INVALID"):
        super().__init__(message)
        self.code = code


def _canonical_uuid(value):
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RawExportError("被评价人筛选不属于当前项目", "SUBJECT_NOT_IN_PROJECT") from exc
    if str(parsed) != str(value):
        raise RawExportError("被评价人筛选不属于当前项目", "SUBJECT_NOT_IN_PROJECT")
    return parsed


def _project_subjects(project, subject_id):
    queryset = ProjectSubject.objects.filter(project=project).order_by("public_id")
    if subject_id is not None:
        parsed = _canonical_uuid(subject_id)
        queryset = queryset.filter(subject__public_id=parsed)
    project_subjects = list(queryset)
    if subject_id is not None and len(project_subjects) != 1:
        raise RawExportError("被评价人筛选不属于当前项目", "SUBJECT_NOT_IN_PROJECT")
    return project_subjects


def _snapshot_map(project_subject):
    subject_snapshot = project_subject.subject_snapshot
    template_snapshot = project_subject.template_snapshot
    relationship_snapshot = project_subject.relationship_snapshot
    if (
        not isinstance(subject_snapshot, dict)
        or set(subject_snapshot)
        != {"public_id", "name", "department_level_1", "department_level_2"}
        or str(project_subject.subject.public_id) != subject_snapshot.get("public_id")
        or not isinstance(template_snapshot, dict)
        or not isinstance(template_snapshot.get("items"), list)
        or not isinstance(relationship_snapshot, list)
    ):
        raise RawExportError("项目冻结快照无效", "RAW_FROZEN_DATA_INVALID")
    items = {}
    for item in template_snapshot["items"]:
        item_id = item.get("snapshot_item_id") if isinstance(item, dict) else None
        if not item_id or item_id in items:
            raise RawExportError("项目冻结快照无效", "RAW_FROZEN_DATA_INVALID")
        items[item_id] = item
    relationships = {}
    for item in relationship_snapshot:
        item_id = item.get("snapshot_item_id") if isinstance(item, dict) else None
        if (
            not item_id
            or item_id in relationships
            or not isinstance(item.get("evaluator"), dict)
        ):
            raise RawExportError("项目冻结快照无效", "RAW_FROZEN_DATA_INVALID")
        relationships[item_id] = item
    return subject_snapshot, items, relationships


def _validated_submission_rows(project, project_subjects):
    project_subject_ids = [row.pk for row in project_subjects]
    submissions = list(
        Submission.objects.filter(
            is_final=True,
            task__project=project,
            task__project_subject_id__in=project_subject_ids,
        )
        .select_related("task", "task__project_subject", "task__subject")
        .prefetch_related("answers")
        .order_by("task__project_subject__public_id", "public_id")
    )
    rows = []
    for project_subject in project_subjects:
        subject_snapshot, items, relationships = _snapshot_map(project_subject)
        subject_submissions = [
            submission
            for submission in submissions
            if submission.task.project_subject_id == project_subject.pk
        ]
        for submission in subject_submissions:
            task = submission.task
            relationship = relationships.get(str(task.relationship_snapshot_item_id))
            answers = list(submission.answers.all())
            answer_ids = [str(answer.item_snapshot_id) for answer in answers]
            if (
                task.project_id != project.pk
                or task.project_subject.project_id != project.pk
                or task.subject_id != project_subject.subject_id
                or relationship is None
                or relationship.get("relationship_type") != task.relationship_type
                or submission.submitted_at is None
                or set(answer_ids) != set(items)
                or len(answer_ids) != len(set(answer_ids))
            ):
                raise RawExportError("正式提交与项目冻结快照不一致", "RAW_SUBMISSION_INVALID")
            rows.append(
                {
                    "project_subject": project_subject,
                    "subject": subject_snapshot,
                    "template_items": items,
                    "relationship": relationship,
                    "submission": submission,
                    "answers": sorted(answers, key=lambda answer: str(answer.item_snapshot_id)),
                }
            )
    return rows


def _description(item, score):
    if score >= 5:
        return item.get("excellent_description", "")
    if score == 4:
        return item.get("good_description", "")
    if score == 3:
        return item.get("qualified_description", "")
    return item.get("improvement_description", "")


def _raw_workbook(project, project_name, row):
    workbook = Workbook()
    workbook.properties.created = FIXED_DOCUMENT_TIME
    workbook.properties.modified = FIXED_DOCUMENT_TIME
    worksheet = workbook.active
    worksheet.title = "原始评价"
    headers = (
        "被评价人",
        "评价人",
        "评价关系",
        "评价项",
        "快照描述",
        "分数",
        "可选备注",
        "提交时间",
    )
    worksheet.append(headers)
    evaluator = row["relationship"]["evaluator"]
    relationship_type = row["relationship"]["relationship_type"]
    submitted_at = row["submission"].submitted_at.isoformat()
    for answer in row["answers"]:
        item = row["template_items"][str(answer.item_snapshot_id)]
        try:
            worksheet.append(
                (
                    safe_cell_text(row["subject"]["name"]),
                    safe_cell_text(evaluator["name"]),
                    safe_cell_text(relationship_type),
                    safe_cell_text(item.get("title", "")),
                    safe_cell_text(_description(item, answer.score)),
                    answer.score,
                    None,
                    submitted_at,
                )
            )
        except OutputBoundaryError as exc:
            raise RawExportError(str(exc), "RAW_OUTPUT_CELL_LIMIT") from exc
    manifest = workbook.create_sheet("项目清单")
    manifest.append(("项目标识", str(project.public_id)))
    manifest.append(("项目名称", safe_cell_text(project_name)))
    manifest.append(("被评价人标识", row["subject"]["public_id"]))
    manifest.append(("模板版本", row["project_subject"].template_snapshot.get("version")))
    output = BytesIO()
    workbook.save(output)
    return _canonical_zip(output.getvalue())


def _zip_info(name):
    info = ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    info.create_system = 3
    return info


def _canonical_zip(payload):
    source = ZipFile(BytesIO(payload))
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as target:
        for name in sorted(source.namelist()):
            member_payload = source.read(name)
            if name == "docProps/core.xml":
                root = ElementTree.fromstring(member_payload)
                modified = root.find(CORE_PROPERTIES_MODIFIED)
                if modified is not None:
                    modified.text = "2000-01-01T00:00:00Z"
                member_payload = ElementTree.tostring(root, encoding="utf-8")
            target.writestr(_zip_info(name), member_payload)
    return output.getvalue()


def _deduplicated_path(base_path, used_paths):
    path = ensure_safe_member_path(base_path)
    if path not in used_paths:
        used_paths.add(path)
        return path
    stem, suffix = path.rsplit(".", 1)
    for index in range(2, 10_000):
        candidate = ensure_safe_member_path(f"{stem}_{index}.{suffix}")
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
    raise RawExportError("原始导出文件名冲突过多", "RAW_FILENAME_COLLISION")


def _member_path(row, used_paths):
    subject_id = row["subject"]["public_id"]
    evaluator_name = safe_filename_component(row["relationship"]["evaluator"]["name"])
    subject_name = safe_filename_component(row["subject"]["name"])
    filename = f"评价人_{evaluator_name}_被评价人_{subject_name}.xlsx"
    try:
        return _deduplicated_path(f"{subject_id}/{filename}", used_paths)
    except OutputBoundaryError as exc:
        raise RawExportError(str(exc), "RAW_FILENAME_INVALID") from exc


def _validate_output_archive(payload):
    if len(payload) > MAX_OUTPUT_ARCHIVE_BYTES:
        raise RawExportError("原始导出压缩包超过字节边界", "RAW_OUTPUT_ARCHIVE_LIMIT")
    archive = ZipFile(BytesIO(payload))
    for info in archive.infolist():
        if info.file_size > MAX_OUTPUT_MEMBER_BYTES:
            raise RawExportError("原始导出成员超过字节边界", "RAW_OUTPUT_BYTE_LIMIT")
        if info.compress_size and info.file_size / info.compress_size > MAX_OUTPUT_COMPRESSION_RATIO:
            raise RawExportError("原始导出成员压缩比超过边界", "RAW_OUTPUT_COMPRESSION_LIMIT")


def export_raw_zip(project, subject_id=None, *, actor=None):
    try:
        require_hr_admin(actor)
    except ValueError as exc:
        raise RawExportError("需要 HR 管理员权限", "HR_ADMIN_REQUIRED") from exc
    project = EvaluationProject.objects.get(pk=project.pk)
    if project.status == EvaluationProject.Status.DRAFT or project.prepared_at is None:
        raise RawExportError("项目尚未准备，不能导出原始数据", "PROJECT_NOT_READY")
    try:
        project_name = frozen_project_name(project)
    except ReportingSnapshotUnavailable as exc:
        raise RawExportError(
            str(exc), "REPORTING_SNAPSHOT_UNAVAILABLE"
        ) from exc
    project_subjects = _project_subjects(project, subject_id)
    rows = _validated_submission_rows(project, project_subjects)
    if len(rows) + 1 > MAX_OUTPUT_MEMBERS:
        raise RawExportError("原始导出成员数量超过边界", "RAW_OUTPUT_MEMBER_LIMIT")

    members = []
    used_paths = set()
    total_bytes = 0
    for row in rows:
        member_payload = _raw_workbook(project, project_name, row)
        if len(member_payload) > MAX_OUTPUT_MEMBER_BYTES:
            raise RawExportError("原始导出成员超过字节边界", "RAW_OUTPUT_BYTE_LIMIT")
        total_bytes += len(member_payload)
        if total_bytes > MAX_OUTPUT_TOTAL_BYTES:
            raise RawExportError("原始导出总字节数超过边界", "RAW_OUTPUT_BYTE_LIMIT")
        path = _member_path(row, used_paths)
        members.append((path, member_payload))
    members.sort(key=lambda item: item[0])
    manifest = {
        "format": 1,
        "project_public_id": str(project.public_id),
        "subject_public_ids": sorted(
            project_subject.subject_snapshot["public_id"]
            for project_subject in project_subjects
        ),
        "members": [
            {
                "path": path,
                "size": len(member_payload),
                "sha256": hashlib.sha256(member_payload).hexdigest(),
            }
            for path, member_payload in members
        ],
    }
    manifest_payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    total_bytes += len(manifest_payload)
    if total_bytes > MAX_OUTPUT_TOTAL_BYTES:
        raise RawExportError("原始导出总字节数超过边界", "RAW_OUTPUT_BYTE_LIMIT")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_zip_info("manifest.json"), manifest_payload)
        for path, member_payload in members:
            archive.writestr(_zip_info(path), member_payload)
    artifact = output.getvalue()
    _validate_output_archive(artifact)
    return artifact
