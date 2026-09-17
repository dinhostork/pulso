"""Disposable PostgreSQL/Redis settings, independent of development credentials."""

import os

from django.core.exceptions import ImproperlyConfigured

from .common import *  # noqa: F403

SECRET_KEY = "test-only-key-not-for-deployment-0123456789-abcdefghijklmnopqrstuvwxyz"
DEBUG = False
# Later local fixture-server integration (#21) uses loopback. Target-policy
# unit tests explicitly exercise both flag values with an injected resolver.
NEWS_FETCH_ALLOW_PRIVATE_NETWORKS = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

TEST_HOST = os.environ.get("TEST_POSTGRES_HOST", "127.0.0.1")
if TEST_HOST not in {"127.0.0.1", "localhost", "postgres-test"}:
    raise ImproperlyConfigured("TEST_POSTGRES_HOST must refer to the local test service")
try:
    TEST_PORT = int(os.environ.get("TEST_POSTGRES_PORT", "55433"))
except ValueError:
    raise ImproperlyConfigured("TEST_POSTGRES_PORT must be a valid port") from None
if not 1 <= TEST_PORT <= 65535:
    raise ImproperlyConfigured("TEST_POSTGRES_PORT must be a valid port")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "pulso_tests",
        "USER": "pulso_tests",
        # Public, disposable credentials for the dedicated postgres-test service only.
        "PASSWORD": "local-only-pulso-tests",
        "HOST": TEST_HOST,
        "PORT": TEST_PORT,
        "OPTIONS": {"connect_timeout": 5},
        "TEST": {"NAME": "test_pulso"},
    }
}

TEST_REDIS_HOST = os.environ.get("TEST_REDIS_HOST", "127.0.0.1")
if TEST_REDIS_HOST not in {"127.0.0.1", "localhost", "redis"}:
    raise ImproperlyConfigured("TEST_REDIS_HOST must refer to the local redis service")
try:
    TEST_REDIS_PORT = int(os.environ.get("TEST_REDIS_PORT", "6399"))
except ValueError:
    raise ImproperlyConfigured("TEST_REDIS_PORT must be a valid port") from None
if not 1 <= TEST_REDIS_PORT <= 65535:
    raise ImproperlyConfigured("TEST_REDIS_PORT must be a valid port")

# Logical DB 1 isolates the opt-in celery_smoke test's broker/result traffic
# from development traffic (DB 0) on the same local Redis instance; ordinary
# tests never open this connection (see pyproject.toml's `-m` default).
CELERY_BROKER_URL = f"redis://{TEST_REDIS_HOST}:{TEST_REDIS_PORT}/1"
CELERY_RESULT_BACKEND = f"redis://{TEST_REDIS_HOST}:{TEST_REDIS_PORT}/1"
# Normal test runs never schedule anything; the Beat diagnostic is a manual
# local verification step, not part of the automated test suite.
CELERY_DIAGNOSTIC_BEAT_ENABLED = False
# News schedules are a deployment concern. These are fixed here rather than
# read from the environment so that NEWS_INGESTION_ENABLED in a developer's
# shell can never make a test run schedule recurring ingestion.
NEWS_INGESTION_ENABLED = False
NEWS_POLL_DISPATCH_INTERVAL_SECONDS = 300
CELERY_BEAT_SCHEDULE = {}

# Tests assert on records from the real JSON formatter inherited from
# config.common. The level is fixed here so a developer's own LOG_LEVEL can
# never hide a record a log test depends on.
LOG_LEVEL = "DEBUG"
LOGGING["loggers"]["pulso"]["level"] = LOG_LEVEL  # noqa: F405
