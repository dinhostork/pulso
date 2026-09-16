import pytest
from django.contrib.auth import get_user_model

from diagnostics.application import execute_diagnostic_ping
from diagnostics.tasks import diagnostic_ping


def test_execute_diagnostic_ping_returns_an_observable_result():
    result = execute_diagnostic_ping()
    assert result["ok"] is True
    assert "executed_at" in result


def test_diagnostic_ping_task_has_a_stable_discoverable_name():
    # Beat schedules and monitoring reference tasks by name; this name must
    # match config/settings.py's CELERY_BEAT_SCHEDULE task path.
    assert diagnostic_ping.name == "diagnostics.tasks.diagnostic_ping"


@pytest.mark.django_db
def test_diagnostic_ping_never_mutates_authoritative_domain_state():
    user_model = get_user_model()
    before = user_model.objects.count()

    # .apply() runs the task synchronously in-process: it exercises the task
    # adapter without a broker or a running worker, simulating the repeat
    # delivery that ADR-0005 says a Celery task may receive.
    first = diagnostic_ping.apply()
    second = diagnostic_ping.apply()

    assert first.successful()
    assert second.successful()
    assert first.result["ok"] is True
    assert second.result["ok"] is True
    assert user_model.objects.count() == before
