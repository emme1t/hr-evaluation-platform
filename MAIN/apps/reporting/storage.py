import os
import tempfile
from pathlib import Path, PurePosixPath

from django.conf import settings


class PrivateStorageError(ValueError):
    pass


def private_root():
    configured = getattr(settings, "REPORTING_PRIVATE_ROOT", None)
    if not configured:
        raise PrivateStorageError("REPORTING_PRIVATE_ROOT 必须显式配置")
    root = Path(configured).resolve()
    repository_root = Path(settings.BASE_DIR).resolve().parent
    if root == repository_root or repository_root in root.parents:
        raise PrivateStorageError("私有存储根目录不能位于应用仓库内")
    return root


def _resolve(relative_path):
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise PrivateStorageError("私有存储路径无效")
    root = private_root()
    target = (root / Path(*pure.parts)).resolve()
    if target != root and root not in target.parents:
        raise PrivateStorageError("私有存储路径越界")
    return root, target


def write_private(relative_path, payload):
    root, target = _resolve(relative_path)
    root.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_private(relative_path):
    _root, target = _resolve(relative_path)
    try:
        return target.read_bytes()
    except OSError as exc:
        raise PrivateStorageError("私有模板文件不可用") from exc


def delete_private(relative_path):
    _root, target = _resolve(relative_path)
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise PrivateStorageError("私有模板文件清理失败") from exc
