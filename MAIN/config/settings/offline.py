import os

from .base import *  # noqa: F403
from .validation import (
    validate_offline_data_root,
    validate_offline_port,
    validate_public_base_url,
)


APP_ENV = "offline"
DEBUG = False
OFFLINE_BIND_HOST = "127.0.0.1"
OFFLINE_PORT = validate_offline_port(os.environ.get("HR_EVAL_PORT", "8765"))
OFFLINE_DATA_ROOT = validate_offline_data_root(
    os.environ.get("HR_EVAL_DATA_ROOT", ""), app_base=BASE_DIR  # noqa: F405
)
OFFLINE_SECRET_PATH = OFFLINE_DATA_ROOT / "secrets" / "django-secret.key"
OFFLINE_LOCK_PATH = OFFLINE_DATA_ROOT / "locks" / "offline.lock"

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
EVALUATION_PUBLIC_BASE_URL = validate_public_base_url(
    f"http://{OFFLINE_BIND_HOST}:{OFFLINE_PORT}",
    app_env=APP_ENV,
    allowed_hosts=ALLOWED_HOSTS,
)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": OFFLINE_DATA_ROOT / "database" / "hr_evaluation.sqlite3",
        "OPTIONS": {
            "timeout": 5,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA busy_timeout=5000;"
                "PRAGMA synchronous=NORMAL"
            ),
        },
    }
}
REPORTING_PRIVATE_ROOT = OFFLINE_DATA_ROOT / "reporting"
EMAIL_BACKEND = "apps.notifications.offline.DisabledOutboundBackend"
EMAIL_TIMEOUT = 5.0
EMAIL_HOST = ""
EMAIL_PORT = 0
EMAIL_USE_TLS = False
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
DEFAULT_FROM_EMAIL = ""
WECOM_CORP_ID = ""
WECOM_AGENT_ID = ""
WECOM_SECRET = ""
