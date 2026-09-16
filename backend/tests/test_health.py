import time

import pytest
from django.test import Client

# A silently-dropping address (TEST-NET-3) proves the probes are actually
# bounded by their configured timeout rather than hanging indefinitely.
UNROUTABLE_HOST = "10.255.255.1"


def test_liveness_returns_200_and_touches_no_dependency(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("liveness must not call dependency probes")

    monkeypatch.setattr("health.views.check_database", fail)
    monkeypatch.setattr("health.views.check_redis", fail)

    response = Client().get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_returns_200_ready_when_dependencies_are_healthy():
    response = Client().get("/health/ready")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["dependencies"]["database"] == {"status": "healthy", "required": True}
    assert body["dependencies"]["redis"] == {"status": "healthy", "required": False}


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_readiness_returns_503_within_the_bounded_timeout_when_database_is_unavailable(settings):
    # Reassign (not mutate in place) so pytest-django's settings fixture
    # restores the original DATABASES dict after the test.
    settings.DATABASES = {
        **settings.DATABASES,
        "default": {**settings.DATABASES["default"], "HOST": UNROUTABLE_HOST},
    }
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS

    started = time.monotonic()
    response = Client().get("/health/ready")
    elapsed = time.monotonic() - started

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "unavailable"
    assert body["dependencies"]["database"]["status"] == "unhealthy"
    assert elapsed < timeout + 2, "database probe exceeded its configured timeout"


@pytest.mark.django_db
def test_readiness_returns_200_degraded_within_the_bounded_timeout_when_redis_is_unavailable(
    settings,
):
    settings.CELERY_BROKER_URL = f"redis://{UNROUTABLE_HOST}:6379/0"
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS

    started = time.monotonic()
    response = Client().get("/health/ready")
    elapsed = time.monotonic() - started

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"]["status"] == "healthy"
    assert body["dependencies"]["redis"] == {"status": "unhealthy", "required": False}
    assert elapsed < timeout + 2, "redis probe exceeded its configured timeout"


@pytest.mark.django_db
def test_readiness_response_omits_credentials_and_internal_details(settings):
    response = Client().get("/health/ready")
    payload = response.content.decode()

    db = settings.DATABASES["default"]
    assert db["PASSWORD"] not in payload
    assert db["HOST"] not in payload
    assert "Traceback" not in payload
    assert 'File "' not in payload


def test_health_endpoints_reject_non_get_methods():
    client = Client()
    assert client.post("/health/live").status_code == 405
    assert client.post("/health/ready").status_code == 405
