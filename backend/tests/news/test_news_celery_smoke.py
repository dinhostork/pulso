"""Opt-in real-broker News ingestion check (#19).

This proves the whole chain with nothing faked: Redis delivers the task to a
separately running worker process, that worker fetches a loopback HTTP server
over real HTTP, and the resulting rows land in the same PostgreSQL database
this test asserts against.

Requirements (see the worker infrastructure section of backend/README.md):

    docker compose --env-file backend/.env.example --profile test \
        up -d --wait postgres-test redis
    cd backend
    DJANGO_SETTINGS_MODULE=config.settings_smoke_worker \
        uv run --locked celery -A config worker --loglevel=INFO --concurrency=2 &
    uv run --locked pytest -m celery_smoke

`config.settings_smoke_worker` exists for exactly one reason: it points the
worker at `test_pulso`, the database pytest-django creates for this session,
instead of the base `pulso_tests` database `config.settings_test` names. The
test is `transaction=True` so its own writes are committed and therefore
visible to the worker's separate connection.

Excluded from default `pytest` runs (see pyproject.toml).
"""

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError

from news.models import Article, IngestionRun, RawArticle, Source, SourceEndpoint
from news.tasks import ingest_endpoint

RESULT_TIMEOUT_SECONDS = 30


def wait_for(result):
    try:
        return result.get(timeout=RESULT_TIMEOUT_SECONDS)
    except CeleryTimeoutError:
        pytest.fail(
            f"No worker consumed news.tasks.ingest_endpoint within "
            f"{RESULT_TIMEOUT_SECONDS}s; start one with the command documented "
            "in backend/README.md (config.settings_smoke_worker)"
        )


@pytest.mark.celery_smoke
@pytest.mark.django_db(transaction=True)
def test_ingestion_completes_through_a_separately_running_worker(fixture_http_server):
    with fixture_http_server("rss_valid.xml") as server:
        source = Source.objects.create(slug="smoke-publisher", name="Smoke Publisher")
        endpoint = SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.RSS, url=server.url
        )

        first = wait_for(ingest_endpoint.delay(endpoint.pk))

        assert first["status"] == IngestionRun.Status.SUCCEEDED
        assert first["will_retry"] is False
        # The worker really fetched the loopback server, in its own process.
        assert server.requests == 1
        articles = Article.objects.filter(source=source).count()
        assert articles == 10
        assert RawArticle.objects.filter(endpoint=endpoint).count() == 10
        run = IngestionRun.objects.get(pk=first["run_id"])
        assert run.endpoint_id == endpoint.pk and run.trigger == "SCHEDULE"
        assert run.raw_created == 10 and run.items_processed == 10

        # Re-delivering the identical feed changes nothing: same payload hashes,
        # no new revision, no new Article (#16 idempotency through the worker).
        second = wait_for(ingest_endpoint.delay(endpoint.pk))

        assert second["status"] == IngestionRun.Status.NO_CHANGE
        assert second["run_id"] != first["run_id"]
        assert server.requests == 2
        assert Article.objects.filter(source=source).count() == articles
        assert RawArticle.objects.filter(endpoint=endpoint).count() == 10
        second_run = IngestionRun.objects.get(pk=second["run_id"])
        assert second_run.raw_unchanged == 10 and second_run.raw_created == 0
