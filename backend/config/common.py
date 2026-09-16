"""Application wiring shared by development and isolated test settings."""

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "accounts.apps.AccountsConfig",
    "database.apps.DatabaseConfig",
    "diagnostics.apps.DiagnosticsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
# The mobile API authentication mechanism belongs to issue #6.
# No authentication adapters or public permissions are enabled implicitly.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Celery: infrastructure only, not domain logic (ADR-0001, ADR-0005). Broker/result
# URLs are environment-specific and defined in settings.py / settings_test.py.
# A single default queue is the documented minimal queue topology for now;
# workload-specific queues/routing are deferred until measured requirements exist.
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
# Finite connection/task timeouts: never block forever on a slow/unreachable
# broker or a stuck task.
CELERY_BROKER_CONNECTION_TIMEOUT = 5
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_TIME_LIMIT = 60
CELERY_TASK_SOFT_TIME_LIMIT = 30
CELERY_RESULT_EXPIRES = 3600
# Tasks may be delivered or executed more than once (ADR-0005); acknowledge
# after completion rather than on receipt, and treat a lost worker as
# redeliverable. Persistent task effects must be idempotent.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
