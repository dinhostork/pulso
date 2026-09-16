"""Reject unsafe database configuration before pytest creates any database."""

import pytest
from django.conf import settings


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if settings.SETTINGS_MODULE != "config.settings_test":
        raise pytest.UsageError(
            "Tests require config.settings_test; development settings are forbidden"
        )
    databases = settings.DATABASES
    if set(databases) != {"default"}:
        raise pytest.UsageError("Tests require exactly one isolated database")
    db = databases["default"]
    expected = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "pulso_tests",
        "USER": "pulso_tests",
        "PASSWORD": "local-only-pulso-tests",
    }
    if any(db.get(key) != value for key, value in expected.items()):
        raise pytest.UsageError("Tests must use the dedicated PostgreSQL test instance")
    if db.get("TEST", {}).get("NAME") != "test_pulso" or db.get("TEST", {}).get("MIRROR"):
        raise pytest.UsageError("Tests must create test_pulso; database mirrors are forbidden")
    if config.getoption("nomigrations", default=False):
        raise pytest.UsageError(
            "Tests require migrations, including the vector extension migration"
        )
