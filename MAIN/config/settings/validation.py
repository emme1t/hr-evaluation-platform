import ipaddress
import math
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured


def _is_loopback(hostname):
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_test_hostname(hostname):
    return hostname in {"example.test", "testserver"} or hostname.endswith(
        (".test", ".example", ".invalid")
    )


def validate_public_base_url(value, *, app_env, allowed_hosts):
    if not isinstance(value, str) or not value.strip():
        raise ImproperlyConfigured("EVALUATION_PUBLIC_BASE_URL must be configured")
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ImproperlyConfigured("EVALUATION_PUBLIC_BASE_URL is invalid")

    hostname = parsed.hostname.lower()
    if app_env == "production":
        normalized_hosts = {str(host).strip().lower() for host in allowed_hosts}
        if (
            parsed.scheme != "https"
            or hostname not in normalized_hosts
            or _is_loopback(hostname)
            or _is_test_hostname(hostname)
        ):
            raise ImproperlyConfigured(
                "EVALUATION_PUBLIC_BASE_URL must be an allowed production HTTPS host"
            )
    elif app_env == "local":
        if parsed.scheme != "http" or not _is_loopback(hostname):
            raise ImproperlyConfigured(
                "EVALUATION_PUBLIC_BASE_URL must be loopback HTTP in local mode"
            )
    elif app_env == "test":
        if not (
            (parsed.scheme == "https" and _is_test_hostname(hostname))
            or (parsed.scheme == "http" and _is_loopback(hostname))
        ):
            raise ImproperlyConfigured(
                "EVALUATION_PUBLIC_BASE_URL must use a test or loopback host"
            )
    elif app_env == "offline":
        if parsed.scheme != "http" or hostname != "127.0.0.1":
            raise ImproperlyConfigured(
                "EVALUATION_PUBLIC_BASE_URL must be literal loopback HTTP offline"
            )
    else:
        raise ImproperlyConfigured("APP_ENV is invalid for notification URLs")

    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def parse_positive_timeout(value, *, setting_name):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ImproperlyConfigured(f"{setting_name} must be a positive number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ImproperlyConfigured(f"{setting_name} must be a positive finite number")
    return timeout


def validate_offline_port(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ImproperlyConfigured("离线端口必须是十进制数字")
    port = int(value)
    if not 1024 <= port <= 65535:
        raise ImproperlyConfigured("离线端口必须在 1024 到 65535 之间")
    return port


def validate_offline_data_root(value: str, *, app_base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ImproperlyConfigured("HR_EVAL_DATA_ROOT 必须配置为绝对路径")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ImproperlyConfigured("HR_EVAL_DATA_ROOT 必须配置为绝对路径")
    try:
        root = candidate.resolve()
        application_root = Path(app_base).resolve()
    except OSError as exc:
        raise ImproperlyConfigured("离线数据目录无法解析") from exc
    if root == application_root or application_root in root.parents:
        raise ImproperlyConfigured("数据目录不能位于应用目录")
    if root in application_root.parents:
        raise ImproperlyConfigured("数据目录不能包含应用目录")

    existing_parent = root
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir() or not os.access(existing_parent, os.W_OK | os.X_OK):
        raise ImproperlyConfigured("离线数据目录无法由当前用户创建")
    return root
