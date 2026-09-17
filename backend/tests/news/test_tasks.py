"""Celery adapter behavior for News ingestion (#19).

Tasks run through `.apply()`: synchronous, in-process, no broker and no
running worker. Celery's eager retry executes the next attempt inline, which
is what makes the whole retry sequence observable here without waiting.
"""

from datetime import timedelta

import pytest
from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from news import tasks
from news.application.ports import FetchError, FetchErrorKind, FetchResult
from news.application.process import ProcessOutcome, ProcessState
from news.domain.fingerprints import payload_hash
from news.models import IngestionRun, RawArticle, Source, SourceEndpoint
from news.tasks import (
    ingest_endpoint,
    poll_due_endpoints,
    process_raw_article,
    reconcile_pending_raw_articles,
)


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))

    class NullFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("news.application.ingest.Fetcher", NullFetcher)
    # Deterministic schedules: the jitter seam is replaced, never the backoff.
    monkeypatch.setattr(tasks, "_jitter", lambda _spread: 0.0)


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="publisher", name="Publisher")
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


def fail_with(monkeypatch, error):
    class FailingAdapter:
        kind = "RSS"

        def fetch(self, request, fetcher):
            raise error

    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: FailingAdapter())


def succeed_with(monkeypatch, result=FetchResult()):
    class StubAdapter:
        kind = "RSS"

        def fetch(self, request, fetcher):
            return result

    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: StubAdapter())


def retry_records(caplog):
    return [
        record for record in caplog.records if record.message == "News ingestion retry scheduled"
    ]


# --- ingest_endpoint task ----------------------------------------------------


@pytest.mark.django_db
def test_timeout_schedules_three_increasing_retries_then_stops(endpoint, monkeypatch, caplog):
    fail_with(monkeypatch, FetchError(FetchErrorKind.TIMEOUT, "Safe", retryable=True))

    ingest_endpoint.apply(args=[endpoint.pk])

    runs = list(IngestionRun.objects.order_by("pk"))
    assert len(runs) == 4
    assert [run.attempt for run in runs] == [0, 1, 2, 3]
    assert [run.trigger for run in runs] == ["SCHEDULE", "RETRY", "RETRY", "RETRY"]
    assert all(run.status == IngestionRun.Status.FAILED for run in runs)
    assert all(run.error_kind == "TIMEOUT" for run in runs)
    # Only the attempts that are actually followed by one say so.
    assert [run.will_retry for run in runs] == [True, True, True, False]

    scheduled = retry_records(caplog)
    assert [record.countdown for record in scheduled] == [30, 60, 120]
    assert [record.attempt for record in scheduled] == [0, 1, 2]
    assert all(record.endpoint_id == endpoint.pk for record in scheduled)
    assert all(record.error_kind == "TIMEOUT" for record in scheduled)
    assert all(record.run_id == run.pk for record, run in zip(scheduled, runs, strict=False))


@pytest.mark.django_db
def test_not_found_fails_once_without_retrying(endpoint, monkeypatch, caplog):
    fail_with(monkeypatch, FetchError(FetchErrorKind.HTTP_STATUS, "Safe", http_status=404))

    result = ingest_endpoint.apply(args=[endpoint.pk])

    run = IngestionRun.objects.get()
    assert run.status == IngestionRun.Status.FAILED
    assert (run.attempt, run.will_retry, run.http_status) == (0, False, 404)
    assert retry_records(caplog) == []
    assert result.result["will_retry"] is False


@pytest.mark.django_db
def test_rate_limited_retry_uses_the_server_delay(endpoint, monkeypatch, caplog):
    fail_with(
        monkeypatch,
        FetchError(
            FetchErrorKind.RATE_LIMITED,
            "Safe",
            retryable=True,
            http_status=429,
            retry_after=120,
        ),
    )

    ingest_endpoint.apply(args=[endpoint.pk])

    scheduled = retry_records(caplog)
    # The server's Retry-After replaces our exponential schedule entirely,
    # including for the later attempts.
    assert [record.countdown for record in scheduled] == [120, 120, 120]
    assert IngestionRun.objects.count() == 4
    assert IngestionRun.objects.order_by("pk").last().will_retry is False


@pytest.mark.django_db
def test_manual_trigger_is_preserved_for_the_first_attempt(endpoint, monkeypatch):
    succeed_with(monkeypatch)

    ingest_endpoint.apply(args=[endpoint.pk], kwargs={"trigger": tasks.TRIGGER_MANUAL})

    run = IngestionRun.objects.get()
    assert (run.trigger, run.attempt) == ("MANUAL", 0)


@pytest.mark.django_db
def test_manual_retries_are_recorded_as_retry(endpoint, monkeypatch):
    fail_with(monkeypatch, FetchError(FetchErrorKind.TIMEOUT, "Safe", retryable=True))

    ingest_endpoint.apply(args=[endpoint.pk], kwargs={"trigger": tasks.TRIGGER_MANUAL})

    assert [run.trigger for run in IngestionRun.objects.order_by("pk")] == [
        "MANUAL",
        "RETRY",
        "RETRY",
        "RETRY",
    ]


@pytest.mark.django_db
def test_task_records_its_celery_task_id(endpoint, monkeypatch):
    succeed_with(monkeypatch)

    ingest_endpoint.apply(args=[endpoint.pk], task_id="task-abc")

    assert IngestionRun.objects.get().task_id == "task-abc"


def test_task_options_bound_ingestion_to_its_own_limits():
    assert ingest_endpoint.max_retries == 3
    assert ingest_endpoint.soft_time_limit == 150
    assert ingest_endpoint.time_limit == 180
    assert process_raw_article.max_retries == 3
    assert ingest_endpoint.name == "news.tasks.ingest_endpoint"
    assert process_raw_article.name == "news.tasks.process_raw_article"
    assert poll_due_endpoints.name == "news.tasks.poll_due_endpoints"
    assert reconcile_pending_raw_articles.name == "news.tasks.reconcile_pending_raw_articles"


@pytest.mark.parametrize("retries", range(8))
def test_backoff_is_bounded_and_increasing_under_real_jitter(retries, monkeypatch):
    monkeypatch.undo()
    current = tasks._backoff_seconds(retries)
    following = tasks._backoff_seconds(retries + 1)
    assert 30 <= current <= 600
    # 10% additive jitter can never overtake the next doubling.
    assert current <= following
    assert tasks._backoff_seconds(retries) >= 30 * 2**retries or current == 600


def test_nonsensical_retry_after_falls_back_to_backoff():
    from news.application.ingest import RunSummary

    def summary(**changes):
        base = dict(
            run_id=1,
            status="FAILED",
            will_retry=True,
            error_kind="RATE_LIMITED",
            items_received=0,
            items_rejected=0,
            raw_created=0,
            raw_unchanged=0,
            raw_changed=0,
            items_processed=0,
            items_failed=0,
            identity_duplicates=0,
            content_duplicates=0,
            raw_rejected=0,
            source_identity_conflicts=0,
        )
        base.update(changes)
        return RunSummary(**base)

    assert tasks._retry_countdown(summary(retry_after=-5), 0) == 30
    assert tasks._retry_countdown(summary(retry_after=None), 1) == 60
    assert tasks._retry_countdown(summary(retry_after=0), 0) == 0
    # A different error kind never consumes a rate-limit delay.
    assert tasks._retry_countdown(summary(error_kind="TIMEOUT", retry_after=120), 0) == 30


# --- process_raw_article task ------------------------------------------------


def make_raw(endpoint, **changes):
    payload = {
        "external_id": "item-1",
        "url": "https://news.example/item",
        "title": "Item one",
        "summary_html": None,
        "content_html": "<p>Body.</p>",
        "published_at": None,
        "updated_at": None,
        "authors": [],
        "language": "en",
        "raw": {},
        "truncated": False,
    }
    payload.update(changes)
    return RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=payload["external_id"] or "key",
        external_id=payload["external_id"] or "",
        url=payload["url"] or "",
        payload=payload,
        payload_hash=payload_hash(payload),
        fetched_at=timezone.now(),
    )


@pytest.mark.django_db
def test_processing_task_returns_a_serializable_outcome(endpoint):
    raw = make_raw(endpoint)

    result = process_raw_article.apply(args=[raw.pk])

    payload = result.result
    assert payload["state"] == "PROCESSED"
    assert payload["outcome"] == "ARTICLE_CREATED"
    assert payload["raw_id"] == raw.pk and payload["article_id"] is not None
    assert __import__("json").dumps(payload)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("state", "outcome", "reason"),
    [
        (ProcessState.REJECTED, "", "MISSING_CANONICAL_URL"),
        (ProcessState.REJECTED, "SOURCE_IDENTITY_CONFLICT", "SOURCE_IDENTITY_CONFLICT"),
        (ProcessState.LOCKED, "", ""),
        (ProcessState.SKIPPED, "", ""),
    ],
)
def test_deterministic_outcomes_are_never_retried(endpoint, monkeypatch, state, outcome, reason):
    calls = []

    def application(raw_id):
        calls.append(raw_id)
        return ProcessOutcome(raw_id=raw_id, state=state, outcome=outcome, rejection_reason=reason)

    monkeypatch.setattr("news.tasks.process_raw_article_app", application)

    result = process_raw_article.apply(args=[7])

    assert calls == [7]
    assert result.result["state"] == str(state)
    assert result.result["rejection_reason"] == reason


@pytest.mark.django_db
def test_operational_error_is_retried_a_bounded_number_of_times(monkeypatch, caplog):
    calls = []

    def application(raw_id):
        calls.append(raw_id)
        raise OperationalError("connection lost")

    monkeypatch.setattr("news.tasks.process_raw_article_app", application)

    result = process_raw_article.apply(args=[11])

    # One initial execution plus exactly max_retries further attempts.
    assert len(calls) == 4
    assert isinstance(result.result, OperationalError)
    records = [
        record
        for record in caplog.records
        if record.message == "News raw processing database error"
    ]
    assert [record.attempt for record in records] == [0, 1, 2, 3]
    assert all(record.raw_id == 11 for record in records)
    assert all(record.exception_class == "OperationalError" for record in records)
    assert not any(
        hasattr(record, field)
        for record in records
        for field in ("payload", "title", "body_text", "connection")
    )


# --- poll_due_endpoints ------------------------------------------------------


@pytest.fixture
def dispatched(monkeypatch):
    """Record ingestion dispatches without touching a broker."""

    calls = []
    monkeypatch.setattr(
        tasks.ingest_endpoint,
        "delay",
        lambda endpoint_id, **kwargs: calls.append((endpoint_id, kwargs)),
    )
    return calls


def make_endpoint(slug, url, *, source_active=True, active=True, interval=900):
    source = Source.objects.create(slug=slug, name=slug.title(), is_active=source_active)
    endpoint = SourceEndpoint.objects.create(
        source=source,
        kind=SourceEndpoint.Kind.RSS,
        url=url,
        fetch_interval_seconds=interval,
    )
    if not active:
        SourceEndpoint.objects.filter(pk=endpoint.pk).update(is_active=False)
        endpoint.refresh_from_db()
    return endpoint


def make_run(endpoint, *, age_seconds, status=IngestionRun.Status.SUCCEEDED):
    started = timezone.now() - timedelta(seconds=age_seconds)
    return IngestionRun.objects.create(
        endpoint=endpoint,
        trigger="SCHEDULE",
        status=status,
        started_at=started,
        finished_at=None if status == IngestionRun.Status.RUNNING else started,
    )


@pytest.mark.django_db
def test_never_ingested_active_endpoint_is_dispatched(dispatched):
    endpoint = make_endpoint("fresh", "https://fresh.example/rss")

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts == {
        "dispatched": 1,
        "skipped_not_due": 0,
        "skipped_running": 0,
        "skipped_inactive": 0,
    }
    assert dispatched == [(endpoint.pk, {"trigger": "SCHEDULE"})]


@pytest.mark.django_db
def test_dispatch_is_deferred_until_the_selecting_transaction_commits(dispatched):
    endpoint = make_endpoint("commit", "https://commit.example/rss")

    with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
        poll_due_endpoints.apply()
        # Selection has finished and the task returned, yet nothing may be
        # queued while the transaction that selected it is still open.
        assert dispatched == []

    assert len(callbacks) == 1
    for callback in callbacks:
        callback()
    assert dispatched == [(endpoint.pk, {"trigger": "SCHEDULE"})]


@pytest.mark.django_db
def test_each_dispatch_carries_its_own_endpoint_id(dispatched):
    first = make_endpoint("one", "https://one.example/rss")
    second = make_endpoint("two", "https://two.example/rss")
    third = make_endpoint("three", "https://three.example/rss")

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["dispatched"] == 3
    # A closure over the loop variable would queue the last id three times.
    assert [endpoint_id for endpoint_id, _ in dispatched] == [first.pk, second.pk, third.pk]


@pytest.mark.django_db
def test_endpoint_due_by_its_own_interval_is_dispatched(dispatched):
    endpoint = make_endpoint("due", "https://due.example/rss", interval=900)
    make_run(endpoint, age_seconds=901)

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["dispatched"] == 1
    assert [endpoint_id for endpoint_id, _ in dispatched] == [endpoint.pk]


@pytest.mark.django_db
def test_endpoint_inside_its_interval_is_not_due(dispatched):
    endpoint = make_endpoint("waiting", "https://waiting.example/rss", interval=900)
    make_run(endpoint, age_seconds=60)

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["skipped_not_due"] == 1 and counts["dispatched"] == 0
    assert dispatched == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("endpoint_active", "source_active"),
    [(False, True), (True, False), (False, False)],
)
def test_inactive_endpoint_or_source_is_never_dispatched(
    dispatched, endpoint_active, source_active
):
    make_endpoint(
        "off",
        "https://off.example/rss",
        active=endpoint_active,
        source_active=source_active,
    )

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts == {
        "dispatched": 0,
        "skipped_not_due": 0,
        "skipped_running": 0,
        "skipped_inactive": 1,
    }
    assert dispatched == []


@pytest.mark.django_db
def test_fresh_running_run_holds_the_endpoint_back(dispatched):
    endpoint = make_endpoint("busy", "https://busy.example/rss", interval=1)
    make_run(endpoint, age_seconds=30, status=IngestionRun.Status.RUNNING)

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["skipped_running"] == 1 and counts["dispatched"] == 0
    assert dispatched == []


@pytest.mark.django_db
def test_stale_running_run_does_not_block_a_due_endpoint(dispatched):
    endpoint = make_endpoint("stalled", "https://stalled.example/rss", interval=900)
    make_run(endpoint, age_seconds=1000, status=IngestionRun.Status.RUNNING)

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["skipped_running"] == 0 and counts["dispatched"] == 1
    assert [endpoint_id for endpoint_id, _ in dispatched] == [endpoint.pk]


@pytest.mark.django_db
def test_only_the_latest_run_decides_whether_an_endpoint_is_due(dispatched):
    endpoint = make_endpoint("history", "https://history.example/rss", interval=900)
    make_run(endpoint, age_seconds=5000)
    make_run(endpoint, age_seconds=4000)
    make_run(endpoint, age_seconds=120)

    with TestCase.captureOnCommitCallbacks(execute=True):
        counts = poll_due_endpoints.apply().result

    assert counts["skipped_not_due"] == 1 and counts["dispatched"] == 0
    assert dispatched == []


# --- reconcile_pending_raw_articles -----------------------------------------


@pytest.fixture
def reconciled(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.process_raw_article, "delay", lambda raw_id: calls.append(raw_id))
    return calls


def age_raw(raw, *, minutes):
    """created_at is auto_now_add, so age is applied with a direct update."""

    RawArticle.objects.filter(pk=raw.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes)
    )
    return raw


@pytest.mark.django_db
def test_old_pending_rows_are_dispatched_and_new_ones_ignored(endpoint, reconciled):
    old = age_raw(make_raw(endpoint, external_id="old"), minutes=11)
    age_raw(make_raw(endpoint, external_id="recent"), minutes=9)

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = reconcile_pending_raw_articles.apply().result

    assert result == {"dispatched": 1}
    assert reconciled == [old.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("status", [RawArticle.Status.PROCESSED, RawArticle.Status.REJECTED])
def test_finalized_rows_are_never_reconciled(endpoint, reconciled, status):
    raw = age_raw(make_raw(endpoint, external_id="done"), minutes=60)
    RawArticle.objects.filter(pk=raw.pk).update(status=status)

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = reconcile_pending_raw_articles.apply().result

    assert result == {"dispatched": 0}
    assert reconciled == []


@pytest.mark.django_db
def test_reconciliation_dispatch_is_deferred_until_commit(endpoint, reconciled):
    raw = age_raw(make_raw(endpoint, external_id="old"), minutes=11)

    with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
        reconcile_pending_raw_articles.apply()
        assert reconciled == []

    for callback in callbacks:
        callback()
    assert reconciled == [raw.pk]


@pytest.mark.django_db
def test_reconciliation_is_bounded_and_ordered_by_receipt(endpoint, reconciled, monkeypatch):
    monkeypatch.setattr(tasks, "PENDING_RECONCILE_LIMIT", 3)
    raws = [make_raw(endpoint, external_id=f"item-{index}") for index in range(5)]
    # The later a row was inserted, the older its receipt: only created_at
    # ordering can select the three oldest, primary-key order cannot.
    for offset, raw in enumerate(raws):
        RawArticle.objects.filter(pk=raw.pk).update(
            created_at=timezone.now() - timedelta(minutes=20 + offset)
        )

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = reconcile_pending_raw_articles.apply().result

    assert result == {"dispatched": 3}
    assert reconciled == [raws[4].pk, raws[3].pk, raws[2].pk]


@pytest.mark.django_db
def test_reconciliation_preserves_receipt_provenance(endpoint, reconciled):
    raw = age_raw(make_raw(endpoint, external_id="old"), minutes=11)
    before = RawArticle.objects.values(
        "payload", "payload_hash", "endpoint_id", "ingestion_run_id", "fetched_at", "supersedes_id"
    ).get(pk=raw.pk)

    with TestCase.captureOnCommitCallbacks(execute=True):
        reconcile_pending_raw_articles.apply()

    assert (
        RawArticle.objects.values(
            "payload",
            "payload_hash",
            "endpoint_id",
            "ingestion_run_id",
            "fetched_at",
            "supersedes_id",
        ).get(pk=raw.pk)
        == before
    )


@pytest.mark.django_db
def test_polling_cost_does_not_grow_with_the_number_of_endpoints(dispatched):
    for index in range(3):
        endpoint = make_endpoint(f"src-{index}", f"https://src{index}.example/rss")
        make_run(endpoint, age_seconds=5000)
    with CaptureQueriesContext(connection) as few:
        with TestCase.captureOnCommitCallbacks(execute=True):
            poll_due_endpoints.apply()

    for index in range(3, 12):
        endpoint = make_endpoint(f"src-{index}", f"https://src{index}.example/rss")
        make_run(endpoint, age_seconds=5000)
    with CaptureQueriesContext(connection) as many:
        with TestCase.captureOnCommitCallbacks(execute=True):
            counts = poll_due_endpoints.apply().result

    assert counts["dispatched"] == 12
    # The latest run per endpoint is an annotation, not a query per endpoint.
    assert len(many.captured_queries) == len(few.captured_queries)
