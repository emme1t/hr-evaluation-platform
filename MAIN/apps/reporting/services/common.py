import re
from pathlib import PurePosixPath


MAX_CELL_TEXT = 32_767
MAX_FILENAME_COMPONENT = 80
FORMULA_PREFIXES = ("=", "+", "-", "@")
PATH_UNSAFE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
SENSITIVE_MARKER = re.compile(
    r"(?:\b(?:token|cookie|authorization|password|secret)\b|"
    r"magic[\s_-]*link|企业微信\s*secret)",
    re.IGNORECASE,
)
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class OutputBoundaryError(ValueError):
    pass


class ReportingSnapshotUnavailable(ValueError):
    pass


def frozen_project_name(project):
    snapshot = getattr(project, "reporting_snapshot", None)
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"name"}
        or not isinstance(snapshot.get("name"), str)
        or not snapshot["name"].strip()
    ):
        raise ReportingSnapshotUnavailable("项目报告快照不可用")
    return snapshot["name"]


def safe_cell_text(value, *, allow_blank=True):
    if value is None:
        return "" if allow_blank else None
    text = str(value)
    if SENSITIVE_MARKER.search(text):
        text = "已移除敏感内容"
    elif text.startswith(FORMULA_PREFIXES):
        text = "'" + text
    if len(text) > MAX_CELL_TEXT:
        raise OutputBoundaryError("单元格文本超过 Excel 32767 字符边界")
    return text


def safe_filename_component(value):
    text = safe_cell_text(value).strip()
    text = PATH_UNSAFE.sub("_", text).strip(" .")
    if not text:
        text = "未命名"
    if text.upper() in WINDOWS_RESERVED:
        text = f"_{text}"
    return text[:MAX_FILENAME_COMPONENT].rstrip(" .") or "未命名"


def ensure_safe_member_path(path):
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
        raise OutputBoundaryError("导出成员路径越界")
    if len(path.encode("utf-8")) > 240:
        raise OutputBoundaryError("导出成员路径超过长度边界")
    return pure.as_posix()
