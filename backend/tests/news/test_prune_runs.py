"""Run-history retention (issue #20): what is deleted, and what must survive."""

from datetime import timedelta
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from news.application.operations import prune_ingestion_runs
from news.domain.fingerprints import payload_hash
from news.models import Article, IngestionRun, RawArticle, Source, SourceEndpoint
from news.tasks import prune_ingestion_runs as prune_task

FINALIZED = (
    IngestionRun.Status.SUCCEEDED,
    IngestionRun.Status.PARTIAL,
    IngestionRun.Status.NO_CHANGE,
    IngestionRun.Status.FAILED,
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="publisher", name="Publisher")
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


def make_run(endpoint, *, days_ago, status=IngestionRun.Status.SUCCEEDED, finished=True):
    moment = timezone.now() - timedelta(days=days_ago)
    return IngestionRun.objects.create(
        endpoint=endpoint,
        trigger="SCHEDULE",
        status=status,
        started_at=moment,
        finished_at=moment if finished else None,
    )


def make_raw(endpoint, run, *, key="item-1"):
    payload = {
        "external_id": key,
        "url": f"https://news.example/{key}",
        "title": "Item",
        "summary_html": None,
        "content_html": "<p>Body.</p>",
        "published_at": None,
        "updated_at": None,
        "authors": [],
        "language": "en",
        "raw": {},
        "truncated": False,
    }
    return RawArticle.objects.create(
        endpoint=endpoint,
        ingestion_run=run,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=key,
        external_id=key,
        url=payload["url"],
        payload=payload,
        payload_hash=payload_hash(payload),
        fetched_at=timezone.now(),
    )


def command(*args):
    out = StringIO()
    call_command("news_prune_runs", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINALIZED)
def test_every_finalized_status_is_pruned_when_old(endpoint, status):
    old = make_run(endpoint, days_ago=45, status=status)

    assert prune_ingestion_runs(days=30) == 1
    assert not IngestionRun.objects.filter(pk=old.pk).exists()


@pytest.mark.django_db
def test_a_running_run_is_never_pruned_whatever_its_age(endpoint):
    ancient = make_run(endpoint, days_ago=400, status=IngestionRun.Status.RUNNING, finished=False)

    assert prune_ingestion_runs(days=30) == 0
    assert IngestionRun.objects.filter(pk=ancient.pk).exists()


@pytest.mark.django_db
def test_a_recent_finalized_run_is_kept(endpoint):
    recent = make_run(endpoint, days_ago=5)

    assert prune_ingestion_runs(days=30) == 0
    assert IngestionRun.objects.filter(pk=recent.pk).exists()


@pytest.mark.django_db
def test_retention_is_measured_from_finished_at_not_started_at(endpoint):
    """A long execution must not expire while it is still running."""

    long_run = IngestionRun.objects.create(
        endpoint=endpoint,
        trigger="SCHEDULE",
        status=IngestionRun.Status.SUCCEEDED,
        started_at=timezone.now() - timedelta(days=60),
        finished_at=timezone.now() - timedelta(days=1),
    )

    assert prune_ingestion_runs(days=30) == 0
    assert IngestionRun.objects.filter(pk=long_run.pk).exists()


@pytest.mark.django_db
def test_a_finalized_run_without_a_finish_timestamp_is_kept(endpoint):
    incomplete = make_run(endpoint, days_ago=90, finished=False)

    assert prune_ingestion_runs(days=30) == 0
    assert IngestionRun.objects.filter(pk=incomplete.pk).exists()


@pytest.mark.django_db
def test_raw_articles_and_articles_survive_with_a_nulled_run_link(endpoint):
    old = make_run(endpoint, days_ago=60)
    raw = make_raw(endpoint, old)
    article = Article.objects.create(
        source=endpoint.source,
        endpoint=endpoint,
        raw_article=raw,
        canonical_url="https://news.example/item-1",
        title="Item",
        language="en",
        content_fingerprint="a" * 64,
        first_seen_at=timezone.now(),
    )

    assert prune_ingestion_runs(days=30) == 1

    raw.refresh_from_db()
    article.refresh_from_db()
    # The FK rule drops the link to discarded history; provenance stays.
    assert raw.ingestion_run_id is None
    assert RawArticle.objects.count() == 1 and Article.objects.count() == 1
    assert raw.payload_hash and raw.endpoint_id == endpoint.pk
    assert article.raw_article_id == raw.pk


@pytest.mark.django_db
def test_only_expired_runs_are_deleted_and_the_count_is_exact(endpoint):
    expired = [make_run(endpoint, days_ago=31 + index) for index in range(3)]
    kept = [
        make_run(endpoint, days_ago=29),
        make_run(endpoint, days_ago=400, status=IngestionRun.Status.RUNNING, finished=False),
    ]

    deleted = prune_ingestion_runs(days=30)

    assert deleted == 3
    assert set(IngestionRun.objects.values_list("pk", flat=True)) == {run.pk for run in kept}
    assert not IngestionRun.objects.filter(pk__in=[run.pk for run in expired]).exists()


@pytest.mark.django_db
def test_the_default_retention_is_thirty_days(endpoint):
    make_run(endpoint, days_ago=29)
    make_run(endpoint, days_ago=31)

    assert settings.NEWS_RUN_RETENTION_DAYS == 30
    assert prune_ingestion_runs() == 1
    assert IngestionRun.objects.count() == 1


@pytest.mark.django_db
def test_a_custom_retention_window_is_honored(endpoint):
    make_run(endpoint, days_ago=3)
    make_run(endpoint, days_ago=10)

    assert prune_ingestion_runs(days=7) == 1
    assert IngestionRun.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("days", [0, -1])
def test_a_nonpositive_retention_is_refused(endpoint, days):
    make_run(endpoint, days_ago=500)

    with pytest.raises(ValueError, match="at least one day"):
        prune_ingestion_runs(days=days)
    assert IngestionRun.objects.count() == 1


@pytest.mark.django_db
def test_pruning_logs_its_outcome_without_run_contents(endpoint, pulso_json_log):
    make_run(endpoint, days_ago=60)

    prune_ingestion_runs(days=30)

    entry = pulso_json_log.entry("News ingestion run history pruned")
    assert entry["logger"] == "pulso.news.operations"
    assert entry["deleted"] == 1 and entry["retention_days"] == 30
    assert "cutoff" in entry
    assert "status" not in entry and "trigger" not in entry


# --- management command ------------------------------------------------------


@pytest.mark.django_db
def test_command_prunes_and_reports_the_count(endpoint):
    make_run(endpoint, days_ago=60)
    make_run(endpoint, days_ago=2)

    output = command("--days", "30")

    assert "Deleted 1 finalized IngestionRun row(s)" in output
    assert "30 day(s)" in output
    assert "RawArticle and Article rows are untouched" in output
    assert IngestionRun.objects.count() == 1


@pytest.mark.django_db
def test_command_defaults_to_the_configured_retention(endpoint):
    make_run(endpoint, days_ago=31)

    output = command()

    assert f"{settings.NEWS_RUN_RETENTION_DAYS} day(s)" in output
    assert IngestionRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0", "-5"])
def test_command_refuses_a_nonpositive_window(endpoint, value):
    make_run(endpoint, days_ago=500)

    with pytest.raises(CommandError, match="--days must be a positive integer"):
        command("--days", value)
    assert IngestionRun.objects.count() == 1


@pytest.mark.django_db
def test_command_output_never_contains_run_contents(endpoint):
    make_run(endpoint, days_ago=60, status=IngestionRun.Status.FAILED)

    output = command()

    assert "FAILED" not in output and "SCHEDULE" not in output


# --- celery task -------------------------------------------------------------


@pytest.mark.django_db
def test_the_task_delegates_to_the_same_application_rule(endpoint):
    make_run(endpoint, days_ago=60)
    make_run(endpoint, days_ago=400, status=IngestionRun.Status.RUNNING, finished=False)

    result = prune_task.apply()

    assert result.result == {"deleted": 1}
    assert IngestionRun.objects.count() == 1
    assert prune_task.name == "news.tasks.prune_ingestion_runs"
