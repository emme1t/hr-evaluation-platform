import os

from .base import *  # noqa: F403
from .validation import validate_public_base_url


APP_ENV = os.environ.get("APP_ENV", "local")
DEBUG = True
SECRET_KEY = os.environ.get("SECRET_KEY", "local-development-secret-key-not-for-production")
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
EVALUATION_PUBLIC_BASE_URL = validate_public_base_url(
    os.environ.get("EVALUATION_PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
    app_env=APP_ENV,
    allowed_hosts=ALLOWED_HOSTS,
)
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_TIMEOUT = 10.0
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_DATABASE", BASE_DIR / "db.sqlite3"),  # noqa: F405
    }
}
