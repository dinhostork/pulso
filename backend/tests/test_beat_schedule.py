"""Beat schedule composition (#19).

`config/settings.py` reads its flags from the environment at import time, so
each case loads that module fresh under an explicit environment. The module is
loaded under a private name and never replaces the active settings.
"""

import importlib.util
import os
import pathlib
from unittest import mock

import pytest
from django.conf import settings as active_settings
from django.core.exceptions import ImproperlyConfigured

from news import tasks

SETTINGS_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "settings.py"

# The minimum required environment; values are public local-development examples.
BASE_ENV = {
    "SECRET_KEY": "local-only-key-for-settings-tests",
    "DEBUG": "true",
    "ALLOWED_HOSTS": "localhost,127.0.0.1",
    "POSTGRES_DB": "pulso",
    "POSTGRES_USER": "pulso",
    "POSTGRES_PASSWORD": "local-only-change-me",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "55432",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6399",
}


def load_settings(**overrides):
    """Import config/settings.py under a controlled, fully explicit environment."""

    spec = importlib.util.spec_from_file_location("config._settings_under_test", SETTINGS_PATH)
    module = importlib.util.module_from_spec(spec)
    # clear=True: a developer's own NEWS_* variables can never leak into a case.
    with mock.patch.dict(os.environ, {**BASE_ENV, **overrides}, clear=True):
        spec.loader.exec_module(module)
    return module


def test_defaults_schedule_both_news_entries_and_no_diagnostic():
    loaded = load_settings()

    assert loaded.NEWS_INGESTION_ENABLED is True
    assert loaded.NEWS_POLL_DISPATCH_INTERVAL_SECONDS == 300
    assert set(loaded.CELERY_BEAT_SCHEDULE) == {
        "news-poll-due-endpoints",
        "news-reconcile-pending",
        "news-prune-runs",
    }
    assert loaded.CELERY_BEAT_SCHEDULE["news-poll-due-endpoints"]["schedule"] == 300.0
    assert loaded.CELERY_BEAT_SCHEDULE["news-reconcile-pending"]["schedule"] == 3600.0


def test_disabling_news_ingestion_removes_every_news_entry():
    loaded = load_settings(NEWS_INGESTION_ENABLED="false")

    assert loaded.NEWS_INGESTION_ENABLED is False
    assert [name for name in loaded.CELERY_BEAT_SCHEDULE if name.startswith("news-")] == []
    assert loaded.CELERY_BEAT_SCHEDULE == {}


def test_poll_interval_is_configurable():
    loaded = load_settings(NEWS_POLL_DISPATCH_INTERVAL_SECONDS="60")

    assert loaded.NEWS_POLL_DISPATCH_INTERVAL_SECONDS == 60
    assert loaded.CELERY_BEAT_SCHEDULE["news-poll-due-endpoints"]["schedule"] == 60.0


@pytest.mark.parametrize("value", ["0", "-1", "abc", "", "3.5"])
def test_invalid_poll_interval_is_a_configuration_error(value):
    with pytest.raises(ImproperlyConfigured, match="must be a positive integer"):
        load_settings(NEWS_POLL_DISPATCH_INTERVAL_SECONDS=value)


@pytest.mark.parametrize("value", ["yes", "1", ""])
def test_invalid_news_ingestion_flag_is_a_configuration_error(value):
    with pytest.raises(ImproperlyConfigured, match="must be true or false"):
        load_settings(NEWS_INGESTION_ENABLED=value)


@pytest.mark.parametrize(
    ("diagnostic", "news", "expected"),
    [
        ("false", "false", set()),
        ("true", "false", {"diagnostic-ping"}),
        (
            "false",
            "true",
            {"news-poll-due-endpoints", "news-reconcile-pending", "news-prune-runs"},
        ),
        (
            "true",
            "true",
            {
                "diagnostic-ping",
                "news-poll-due-endpoints",
                "news-reconcile-pending",
                "news-prune-runs",
            },
        ),
    ],
)
def test_the_two_schedule_flags_are_independent(diagnostic, news, expected):
    loaded = load_settings(CELERY_DIAGNOSTIC_BEAT_ENABLED=diagnostic, NEWS_INGESTION_ENABLED=news)

    assert set(loaded.CELERY_BEAT_SCHEDULE) == expected


def test_scheduled_task_names_exist():
    loaded = load_settings(CELERY_DIAGNOSTIC_BEAT_ENABLED="true")
    scheduled = {entry["task"] for entry in loaded.CELERY_BEAT_SCHEDULE.values()}

    assert scheduled == {
        "diagnostics.tasks.diagnostic_ping",
        tasks.poll_due_endpoints.name,
        tasks.reconcile_pending_raw_articles.name,
        tasks.prune_ingestion_runs.name,
    }


def test_test_settings_never_schedule_anything():
    # Whatever the environment says, an ordinary test run schedules no jobs.
    assert active_settings.CELERY_BEAT_SCHEDULE == {}
    assert active_settings.NEWS_INGESTION_ENABLED is False
    assert active_settings.CELERY_DIAGNOSTIC_BEAT_ENABLED is False
