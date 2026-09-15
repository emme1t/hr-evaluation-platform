import os

from .base import *  # noqa: F403
from .validation import parse_positive_timeout, validate_public_base_url


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be configured for production")
    return value


APP_ENV = "production"
DEBUG = False
SECRET_KEY = required_env("SECRET_KEY")
ALLOWED_HOSTS = [
    host.strip() for host in required_env("ALLOWED_HOSTS").split(",") if host.strip()
]
EVALUATION_PUBLIC_BASE_URL = validate_public_base_url(
    required_env("EVALUATION_PUBLIC_BASE_URL"),
    app_env=APP_ENV,
    allowed_hosts=ALLOWED_HOSTS,
)
REPORTING_PRIVATE_ROOT = required_env("REPORTING_PRIVATE_ROOT")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required_env("POSTGRES_DB"),
        "USER": required_env("POSTGRES_USER"),
        "PASSWORD": required_env("POSTGRES_PASSWORD"),
        "HOST": required_env("POSTGRES_HOST"),
        "PORT": required_env("POSTGRES_PORT"),
    }
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("SMTP_HOST", "")
EMAIL_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.environ.get("SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("SMTP_FROM", EMAIL_HOST_USER)
EMAIL_TIMEOUT = parse_positive_timeout(
    os.environ.get("EMAIL_TIMEOUT", "10"), setting_name="EMAIL_TIMEOUT"
)

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
