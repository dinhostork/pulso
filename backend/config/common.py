"""Application wiring shared by development and isolated test settings."""

from datetime import timedelta

from .environment import log_level

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "accounts.apps.AccountsConfig",
    "database.apps.DatabaseConfig",
    "diagnostics.apps.DiagnosticsConfig",
    "news.apps.NewsConfig",
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
# Mobile API authentication (issue #6, ADR-0009): stateless JWT bearer
# tokens, not cookie/session auth, so CSRF protection does not apply to
# these endpoints. No public permissions are enabled implicitly; every view
# is IsAuthenticated unless it explicitly opts out (login, logout, refresh).
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
# See ADR-0009 for the lifetime/rotation rationale, including the documented
# residual-validity window: logout blacklists the refresh token immediately,
# but an already-issued, not-yet-expired access token keeps working until it
# expires naturally (JWTs are verified statelessly, not looked up per request).
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": False,
    "UPDATE_LAST_LOGIN": True,
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

# Bounded timeout for /health/ready's dependency probes (issue #5): a slow or
# unreachable PostgreSQL/Redis must fail fast, not hang the request.
HEALTH_CHECK_TIMEOUT_SECONDS = 2

# News outbound transport policy. Only the private-network switch is an
# environment option; the other limits are fixed for predictable behavior.
NEWS_FETCH_ALLOW_PRIVATE_NETWORKS = False
NEWS_FETCH_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
NEWS_FETCH_MAX_REDIRECTS = 5
NEWS_FETCH_CONNECT_TIMEOUT_SECONDS = 5
NEWS_FETCH_READ_TIMEOUT_SECONDS = 15
NEWS_FETCH_WRITE_TIMEOUT_SECONDS = 5
NEWS_FETCH_POOL_TIMEOUT_SECONDS = 5
NEWS_INGEST_MAX_ITEMS_PER_RUN = 500
NEWS_INGEST_MAX_PAYLOAD_BYTES = 256 * 1024

# Minimum normalized fingerprint-input length that makes exact content equality
# usable evidence of a republication (ADR-0010 Tier 2). Below it, short generic
# titles with no body would link unrelated publications; a fixed application
# constant, not an environment knob.
NEWS_CONTENT_FINGERPRINT_MIN_CHARS = 200

# Story semantic embeddings (#25). `NEWS_EMBEDDING_PROVIDER` names one provider
# (`local` or `deterministic`) and carries no options, so no credential can be
# configured for it. The bounds are fixed application constants: the batch
# size caps one provider call, and each Article input is cut to at most
# NEWS_EMBEDDING_MAX_INPUT_CHARS code points (see news/domain/embeddings.py).
NEWS_EMBEDDING_PROVIDER = "local"
NEWS_EMBEDDING_MAX_BATCH = 32
NEWS_EMBEDDING_MAX_INPUT_CHARS = 2000

# Story candidate retrieval (#27). These are recall bounds that cap the work
# one Article can cause; they are deliberately generous and are NOT the match
# threshold, which the matching decision applies to the returned distances.
# If tuning them changes match outcomes, the threshold has leaked into
# retrieval. Distance is pgvector cosine distance (0 identical, 2 opposite);
# the window is in hours around the Article's publication time and must
# overlap a Story's member publication range.
NEWS_STORY_CANDIDATE_LIMIT = 10
NEWS_STORY_CANDIDATE_MAX_DISTANCE = 0.5
NEWS_STORY_CANDIDATE_WINDOW_HOURS = 168

# One RUNNING ingestion run younger than this is assumed to be in flight: the
# poll dispatcher skips its endpoint (#19) and `news_runs --stale` does not
# report it (#20). One fixed operational constant, shared so the two views can
# never disagree; not an environment knob.
NEWS_STALE_RUNNING_SECONDS = 180
# Default retention for finalized IngestionRun history, used by both
# `news_prune_runs` and the optional weekly prune task (#20).
NEWS_RUN_RETENTION_DAYS = 30

# Structured operational logging (issue #20). Only the `pulso` tree is
# configured: Django's and Celery's own loggers keep their default behavior,
# including their tracebacks. Records are JSON lines carrying operational
# identifiers, statuses and counts — never publication content (see
# config/logging.py's allowlist).
LOG_LEVEL = log_level("LOG_LEVEL")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "pulso_json": {"()": "config.logging.JsonLinesFormatter"},
    },
    "handlers": {
        "pulso_console": {
            "class": "logging.StreamHandler",
            "formatter": "pulso_json",
        },
    },
    "loggers": {
        "pulso": {
            "handlers": ["pulso_console"],
            "level": LOG_LEVEL,
            # Structured records must not also reach the root handler as plain
            # text. Tests attach their own handler to this logger instead of
            # relying on propagation (see tests/news/conftest.py).
            "propagate": False,
        },
    },
}
