"""Opt-in real-broker smoke check.

Requires a running Redis (`docker compose --profile test up -d redis` or the
development `redis` service) and a separately running Celery worker started
with `config.settings_test` (see the worker infrastructure section of
backend/README.md). Eager/local execution does not exercise this path.

Excluded from default `pytest` runs (see pyproject.toml); run explicitly:

    uv run --locked pytest -m celery_smoke
"""

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from django.contrib.auth import get_user_model

from diagnostics.tasks import diagnostic_ping

RESULT_TIMEOUT_SECONDS = 10


@pytest.mark.celery_smoke
@pytest.mark.django_db
def test_diagnostic_ping_completes_through_a_separately_running_worker():
    user_model = get_user_model()
    before = user_model.objects.count()

    try:
        first = diagnostic_ping.delay().get(timeout=RESULT_TIMEOUT_SECONDS)
        second = diagnostic_ping.delay().get(timeout=RESULT_TIMEOUT_SECONDS)
    except CeleryTimeoutError:
        pytest.fail(
            f"No worker consumed the task within {RESULT_TIMEOUT_SECONDS}s; "
            "start one with the command documented in README.md"
        )

    assert first["ok"] is True
    assert second["ok"] is True
    # Real delivery through Redis to a separate worker process, repeated:
    # still no authoritative domain state changes (ADR-0005).
    assert user_model.objects.count() == before
