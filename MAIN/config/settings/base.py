import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
APP_ENV = os.environ.get("APP_ENV", "local")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.core.apps.CoreConfig",
    "apps.roster.apps.RosterConfig",
    "apps.evaluations.apps.EvaluationsConfig",
    "apps.notifications.apps.NotificationsConfig",
    "apps.reporting.apps.ReportingConfig",
    "apps.audit.apps.AuditConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.audit.middleware.AuditRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

# 1000 frozen score items plus CSRF/idempotency fields, with bounded headroom.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1100

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID", "")
WECOM_SECRET = os.environ.get("WECOM_SECRET", "")
WECOM_OAUTH_STATE_TTL_SECONDS = int(os.environ.get("WECOM_OAUTH_STATE_TTL_SECONDS", "600"))
EVALUATION_PUBLIC_BASE_URL = os.environ.get("EVALUATION_PUBLIC_BASE_URL", "")
REPORTING_PRIVATE_ROOT = os.environ.get("REPORTING_PRIVATE_ROOT")
AUDIT_RUNTIME_DB_ROLE = os.environ.get("AUDIT_RUNTIME_DB_ROLE", "")
