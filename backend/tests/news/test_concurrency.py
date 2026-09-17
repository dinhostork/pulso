"""Race arbitration by PostgreSQL uniqueness, not application locking (ADR-0010).

Two RawArticles are processed on genuinely separate database connections. Both
read their candidates before either inserts, so the database decides the winner
and the loser must resolve deterministically without an uncaught uniqueness
error and without ever re-attributing the winning Article.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connections
from django.utils import timezone

from news.application import process as process_module
from news.application.process import ProcessState, process_raw_article
from news.domain.fingerprints import payload_hash
from news.models import Article, RawArticle, Source, SourceEndpoint

RACED_URL = "https://news.example/contested"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


def make_endpoint(slug, url):
    source = Source.objects.create(slug=slug, name=slug.title())
    return SourceEndpoint.objects.create(source=source, kind=SourceEndpoint.Kind.RSS, url=url)


def make_raw(endpoint, external_id):
    """A pending revision that normalizes to the contested canonical URL."""

    payload = {
        "external_id": external_id,
        "url": RACED_URL,
        "title": "Contested publication",
        "summary_html": None,
        "content_html": "<p>Contested body.</p>",
        "published_at": None,
        "updated_at": None,
        "authors": [],
        "language": "en",
        "raw": {},
        "truncated": False,
    }
    return RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=external_id,
        external_id=external_id,
        url=RACED_URL,
        payload=payload,
        payload_hash=payload_hash(payload),
        fetched_at=timezone.now(),
    )


def backend_pid():
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def race(monkeypatch, raw_ids):
    """Run processing concurrently, held at a barrier until both have decided."""

    barrier = threading.Barrier(len(raw_ids), timeout=30)
    insert = process_module._insert

    def barriered(*args, **kwargs):
        # Both workers have read their candidates by the time they arrive here.
        barrier.wait()
        return insert(*args, **kwargs)

    monkeypatch.setattr(process_module, "_insert", barriered)

    def worker(raw_id):
        close_old_connections()
        try:
            return process_raw_article(raw_id), backend_pid()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(raw_ids)) as pool:
        futures = [pool.submit(worker, raw_id) for raw_id in raw_ids]
        return [future.result(timeout=60) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_same_source_race_creates_one_article_and_one_identity_duplicate(monkeypatch):
    endpoint = make_endpoint("publisher", "https://feed.example/rss")
    first = make_raw(endpoint, "race-a")
    second = make_raw(endpoint, "race-b")

    results = race(monkeypatch, [first.pk, second.pk])

    outcomes = [outcome for outcome, _ in results]
    pids = {pid for _, pid in results}
    assert len(pids) == 2 and backend_pid() not in pids
    assert all(outcome.state is ProcessState.PROCESSED for outcome in outcomes)
    assert Article.objects.count() == 1
    article = Article.objects.get()
    assert article.canonical_url == RACED_URL
    assert sorted(outcome.outcome for outcome in outcomes) == [
        RawArticle.Outcome.ARTICLE_CREATED,
        RawArticle.Outcome.IDENTITY_DUPLICATE,
    ]
    assert {outcome.article_id for outcome in outcomes} == {article.pk}
    assert set(RawArticle.objects.values_list("status", flat=True)) == {RawArticle.Status.PROCESSED}


@pytest.mark.django_db(transaction=True)
def test_cross_source_race_records_source_identity_conflict(monkeypatch):
    first_endpoint = make_endpoint("publisher", "https://feed.example/rss")
    second_endpoint = make_endpoint("aggregator", "https://aggregator.example/rss")
    first = make_raw(first_endpoint, "race-a")
    second = make_raw(second_endpoint, "race-b")

    results = race(monkeypatch, [first.pk, second.pk])

    pids = {pid for _, pid in results}
    assert len(pids) == 2 and backend_pid() not in pids
    assert Article.objects.count() == 1
    article = Article.objects.select_related("source").get()
    created = next(
        outcome for outcome, _ in results if outcome.outcome == RawArticle.Outcome.ARTICLE_CREATED
    )
    conflicted = next(
        outcome
        for outcome, _ in results
        if outcome.outcome == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    )
    assert created.article_id == article.pk
    assert conflicted.state is ProcessState.REJECTED
    assert conflicted.rejection_reason == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    assert conflicted.article_id == article.pk
    # The Article belongs to whichever insert the database accepted, and the
    # losing Source never becomes its owner.
    winner = RawArticle.objects.get(pk=created.raw_id)
    loser = RawArticle.objects.get(pk=conflicted.raw_id)
    assert article.source_id == winner.endpoint.source_id
    assert article.endpoint_id == winner.endpoint_id
    assert article.raw_article_id == winner.pk
    assert loser.endpoint.source_id != article.source_id
    assert loser.status == RawArticle.Status.REJECTED


@pytest.mark.django_db(transaction=True)
def test_loser_updates_nothing_on_the_winning_article(monkeypatch):
    first_endpoint = make_endpoint("publisher", "https://feed.example/rss")
    second_endpoint = make_endpoint("aggregator", "https://aggregator.example/rss")
    first = make_raw(first_endpoint, "race-a")
    second = make_raw(second_endpoint, "race-b")

    results = race(monkeypatch, [first.pk, second.pk])
    loser = RawArticle.objects.select_related("endpoint").get(
        pk=next(
            outcome.raw_id
            for outcome, _ in results
            if outcome.outcome == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
        )
    )
    article = Article.objects.get()
    before = {
        field: getattr(article, field)
        for field in (
            "source_id",
            "endpoint_id",
            "raw_article_id",
            "updated_at",
            "canonical_url",
            "title",
            "body_text",
            "external_id",
            "duplicate_of_id",
        )
    }

    # Re-delivering the same URL from the Source that lost changes nothing.
    again = make_raw(loser.endpoint, "race-c")
    outcome = process_raw_article(again.pk)

    article.refresh_from_db()
    assert outcome.rejection_reason == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    assert before == {field: getattr(article, field) for field in before}
    assert Article.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_workers_never_process_one_revision_twice(monkeypatch):
    """`of=("self",)` narrows the lock to the revision without losing it."""

    endpoint = make_endpoint("publisher", "https://feed.example/rss")
    raw = make_raw(endpoint, "race-a")
    reached_insert = threading.Event()
    release = threading.Event()
    insert = process_module._insert

    def held(*args, **kwargs):
        reached_insert.set()
        assert release.wait(timeout=30)
        return insert(*args, **kwargs)

    monkeypatch.setattr(process_module, "_insert", held)

    def worker(raw_id):
        close_old_connections()
        try:
            return process_raw_article(raw_id)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(worker, raw.pk)
        assert reached_insert.wait(timeout=30)
        contender = pool.submit(worker, raw.pk)
        contended = contender.result(timeout=30)
        release.set()
        held_outcome = holder.result(timeout=60)

    assert held_outcome.state is ProcessState.PROCESSED
    assert held_outcome.outcome == RawArticle.Outcome.ARTICLE_CREATED
    assert contended.state is ProcessState.LOCKED
    assert Article.objects.count() == 1
    assert RawArticle.objects.get().status == RawArticle.Status.PROCESSED
