"""Disposable PostgreSQL settings, independent of development credentials."""

import os

from django.core.exceptions import ImproperlyConfigured

from .common import *  # noqa: F403

SECRET_KEY = "test-only-key-not-for-deployment-0123456789-abcdefghijklmnopqrstuvwxyz"
DEBUG = False
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
