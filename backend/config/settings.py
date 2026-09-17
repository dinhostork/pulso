"""Shared settings. Local development must explicitly opt into DEBUG."""

from django.core.exceptions import ImproperlyConfigured

from .common import *  # noqa: F403
from .environment import boolean, port, positive_int, required

SECRET_KEY = required("SECRET_KEY")
DEBUG = boolean("DEBUG")
NEWS_FETCH_ALLOW_PRIVATE_NETWORKS = boolean("NEWS_FETCH_ALLOW_PRIVATE_NETWORKS")
ALLOWED_HOSTS = [host.strip() for host in required("ALLOWED_HOSTS").split(",")]
if any(not host for host in ALLOWED_HOSTS) or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must list explicit nonempty hosts")
if not DEBUG and (
    len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY.startswith(("local-only-", "django-insecure-"))
):
    raise ImproperlyConfigured("SECRET_KEY must be a strong non-example key when DEBUG=false")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required("POSTGRES_DB"),
        "USER": required("POSTGRES_USER"),
        "PASSWORD": required("POSTGRES_PASSWORD"),
        "HOST": required("POSTGRES_HOST"),
        "PORT": port("POSTGRES_PORT"),
        "OPTIONS": {"connect_timeout": 5},
    }
}
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

REDIS_HOST = required("REDIS_HOST")
REDIS_PORT = port("REDIS_PORT")
# Redis is a broker/result cache, not authoritative persistence (ADR-0005).
# Broker and result backend share logical DB 0; there is only one process
# group using it outside of tests, so separate DB indices add no isolation
# value here (see settings_test.py for the isolated test DB index).
CELERY_BROKER_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
CELERY_RESULT_BACKEND = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

# Disabled by default: the diagnostic schedule is an infrastructure probe, not
# a product job. Enabling it verifies Celery Beat locally with the harmless
# diagnostic task; see the worker infrastructure section of README.md.
CELERY_DIAGNOSTIC_BEAT_ENABLED = boolean("CELERY_DIAGNOSTIC_BEAT_ENABLED")
# News ingestion schedules (issue #19). Enabled by default: a deployment that
# runs Beat is expected to poll its configured endpoints. The two flags are
# independent; neither one enables or disables the other's entries.
NEWS_INGESTION_ENABLED = boolean("NEWS_INGESTION_ENABLED", default=True)
NEWS_POLL_DISPATCH_INTERVAL_SECONDS = positive_int(
    "NEWS_POLL_DISPATCH_INTERVAL_SECONDS", default=300
)

CELERY_BEAT_SCHEDULE = {}
if CELERY_DIAGNOSTIC_BEAT_ENABLED:
    CELERY_BEAT_SCHEDULE["diagnostic-ping"] = {
        "task": "diagnostics.tasks.diagnostic_ping",
        "schedule": 30.0,
    }
if NEWS_INGESTION_ENABLED:
    CELERY_BEAT_SCHEDULE["news-poll-due-endpoints"] = {
        "task": "news.tasks.poll_due_endpoints",
        "schedule": float(NEWS_POLL_DISPATCH_INTERVAL_SECONDS),
    }
    CELERY_BEAT_SCHEDULE["news-reconcile-pending"] = {
        "task": "news.tasks.reconcile_pending_raw_articles",
        "schedule": 3600.0,
    }
