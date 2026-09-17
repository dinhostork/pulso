"""Cross-layer News Core contracts over real loopback HTTP and PostgreSQL."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta

import pytest
from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from news.application import ingest as ingest_module
from news.application.ingest import ingest_endpoint
from news.application.operations import stale_running_runs
from news.application.process import ProcessState
from news.models import Article, IngestionRun, RawArticle, Source, SourceEndpoint
from news.tasks import PENDING_RECONCILE_AFTER_SECONDS, reconcile_pending_raw_articles


def source(slug):
    return Source.objects.create(slug=slug, name=slug.title(), default_language="")


def endpoint(owner, server, kind="RSS"):
    return SourceEndpoint.objects.create(source=owner, kind=kind, url=server.url)


def ingest(target):
    return ingest_endpoint(target.pk, trigger="MANUAL")


def state_signature():
    """Authoritative identity and processing state, excluding operational IDs/times."""

    raws = frozenset(
        (
            raw.endpoint.source.slug,
            raw.endpoint.kind,
            raw.external_key_kind,
            raw.external_key,
            raw.payload_hash,
            raw.status,
            raw.outcome,
            raw.rejection_reason,
        )
        for raw in RawArticle.objects.select_related("endpoint__source")
    )
    articles = frozenset(
        (
            article.source.slug,
            article.canonical_url,
            article.external_id,
            article.content_fingerprint,
            article.duplicate_of.canonical_url if article.duplicate_of_id else None,
        )
        for article in Article.objects.select_related("source", "duplicate_of")
    )
    return raws, articles


def assert_identity_invariants():
    for raw in RawArticle.objects.all():
        expected = "EXTERNAL_ID" if raw.external_id else "CANONICAL_URL"
        assert raw.external_key_kind == expected
    for article in Article.objects.select_related(
        "source", "endpoint__source", "raw_article__ingestion_run__endpoint"
    ):
        assert article.canonical_url
        assert len(article.content_fingerprint) == 64
        assert article.source_id == article.endpoint.source_id
        assert article.raw_article.endpoint_id == article.endpoint_id
        assert article.raw_article.ingestion_run.endpoint_id == article.endpoint_id
        assert article.raw_article.article_id == article.pk


@pytest.mark.django_db
def test_primary_rss_and_json_corpus_repeat_and_provenance(fixture_http_server):
    with ExitStack() as stack:
        rss_server = stack.enter_context(fixture_http_server("rss_valid.xml"))
        json_server = stack.enter_context(
            fixture_http_server("jsonfeed_valid.json", "application/feed+json")
        )
        publisher = source("primary")
        rss = endpoint(publisher, rss_server)
        json_feed = endpoint(publisher, json_server, "JSON_FEED")
        rss_run = ingest(rss)
        json_run = ingest(json_feed)

        # RSS: 10 new; JSON: 6 accepted, including two story-1 revisions,
        # one id-only rejection, one mailto identity-less intake rejection,
        # and overlap with RSS story-1/story-2.
        assert (rss_run.items_received, rss_run.raw_created) == (10, 10)
        assert json_run.items_received == 6
        assert (json_run.raw_created, json_run.raw_changed, json_run.items_rejected) == (4, 1, 1)
        assert json_run.raw_rejected == 1
        assert (
            json_run.identity_duplicates,
            json_run.content_duplicates,
            json_run.source_identity_conflicts,
        ) == (0, 0, 0)
        assert RawArticle.objects.count() == 15
        assert Article.objects.count() == 11
        assert IngestionRun.objects.count() == 2
        assert RawArticle.objects.filter(status="REJECTED").count() == 1
        assert set(Article.objects.values_list("canonical_url", flat=True)) == {
            *(f"https://news.example/story/{number}" for number in range(1, 11)),
            "https://news.example/invalid-date",
        }
        assert Article.objects.filter(duplicate_of__isnull=False).count() == 0
        assert Article.objects.get(canonical_url="https://news.example/story/1").title == (
            "Duplicate provider ID"
        )
        assert_identity_invariants()
        assert rss_server.requests == json_server.requests == 1

        before = state_signature()
        second_rss = ingest(rss)
        second_json = ingest(json_feed)
        assert RawArticle.objects.count() == 15 and Article.objects.count() == 11
        assert IngestionRun.objects.count() == 4
        assert second_rss.raw_unchanged == 10
        assert second_json.raw_unchanged == 5
        assert state_signature() == before
        assert rss_server.requests == json_server.requests == 2


@pytest.mark.django_db
def test_changed_revision_same_endpoint_updates_article(fixture_http_server):
    with fixture_http_server("rss_changed_item_v1.xml") as server:
        target = endpoint(source("revision"), server)
        ingest(target)
        first = RawArticle.objects.get()
        article = Article.objects.get()
        before = (article.pk, article.first_seen_at, article.source_id)
        old_fingerprint = article.content_fingerprint
        server.set_fixture("rss_changed_item_v2.xml")
        ingest(target)
        article.refresh_from_db()
        second = RawArticle.objects.latest("pk")
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (2, 1, 2)
        assert second.supersedes_id == first.pk
        assert second.outcome == "ARTICLE_UPDATED" and article.raw_article_id == second.pk
        assert (article.pk, article.first_seen_at, article.source_id) == before
        assert article.content_fingerprint != old_fingerprint
        assert (article.title, article.body_text) == ("Version two", "Second body.")


@pytest.mark.django_db
def test_id_only_is_retained_but_identity_less_is_not(fixture_http_server):
    with fixture_http_server("rss_id_only_entry.xml") as server:
        target = endpoint(source("identity"), server)
        summary = ingest(target)
        raw = RawArticle.objects.get()
        assert (summary.items_received, summary.items_rejected, summary.raw_rejected) == (2, 1, 1)
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (1, 0, 1)
        assert raw.external_id == "guid-only-1" and raw.external_key_kind == "EXTERNAL_ID"
        assert raw.status == "REJECTED" and raw.rejection_reason == "MISSING_CANONICAL_URL"
        assert raw.payload["external_id"] == "guid-only-1"


@pytest.mark.django_db
def test_second_endpoint_and_cross_source_conflicts_preserve_owner(fixture_http_server):
    with ExitStack() as stack:
        primary_server = stack.enter_context(fixture_http_server("rss_valid.xml"))
        mirror_server = stack.enter_context(
            fixture_http_server("rss_same_canonical_other_endpoint.xml")
        )
        other_server = stack.enter_context(
            fixture_http_server("rss_same_canonical_other_source.xml")
        )
        publisher = source("publisher")
        ingest(endpoint(publisher, primary_server))
        owned = {
            article.canonical_url: tuple(
                getattr(article, field)
                for field in (
                    "source_id",
                    "endpoint_id",
                    "raw_article_id",
                    "canonical_url",
                    "external_id",
                    "title",
                    "description",
                    "body_text",
                    "content_fingerprint",
                    "updated_at",
                )
            )
            for article in Article.objects.all()
        }
        mirror = endpoint(publisher, mirror_server)
        mirror_run = ingest(mirror)
        assert mirror_run.identity_duplicates == 2
        assert RawArticle.objects.filter(endpoint=mirror, outcome="IDENTITY_DUPLICATE").count() == 2
        other = endpoint(source("aggregator"), other_server)
        conflict_run = ingest(other)
        conflicts = list(RawArticle.objects.filter(endpoint=other))
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (14, 10, 3)
        assert (
            len(conflicts)
            == conflict_run.raw_rejected
            == conflict_run.source_identity_conflicts
            == 2
        )
        assert all(
            raw.status == "REJECTED"
            and raw.outcome == raw.rejection_reason == "SOURCE_IDENTITY_CONFLICT"
            for raw in conflicts
        )
        assert all(raw.article_id is not None for raw in conflicts)
        for article in Article.objects.all():
            assert owned[article.canonical_url] == tuple(
                getattr(article, field)
                for field in (
                    "source_id",
                    "endpoint_id",
                    "raw_article_id",
                    "canonical_url",
                    "external_id",
                    "title",
                    "description",
                    "body_text",
                    "content_fingerprint",
                    "updated_at",
                )
            )


@pytest.mark.django_db
def test_syndication_and_same_event_boundaries(fixture_http_server):
    with ExitStack() as stack:
        origin_server = stack.enter_context(fixture_http_server("rss_syndication_origin.xml"))
        copy_server = stack.enter_context(fixture_http_server("rss_syndicated_copy.xml"))
        event_server = stack.enter_context(
            fixture_http_server("rss_same_story_different_articles.xml")
        )
        ingest(endpoint(source("origin"), origin_server))
        ingest(endpoint(source("syndicate"), copy_server))
        ingest(endpoint(source("events"), event_server))
        origin = Article.objects.get(source__slug="origin")
        copy = Article.objects.get(source__slug="syndicate")
        events = list(Article.objects.filter(source__slug="events"))
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (5, 5, 3)
        assert origin.canonical_url != copy.canonical_url
        assert origin.content_fingerprint == copy.content_fingerprint
        assert copy.duplicate_of_id == origin.pk
        assert len(events) == len({article.canonical_url for article in events}) == 3
        assert len({article.content_fingerprint for article in events}) == 3
        assert all(article.duplicate_of_id is None for article in events)
        assert_identity_invariants()


class SimulatedWorkerCrash(BaseException):
    pass


@pytest.mark.django_db
def test_crash_after_intake_redelivery_matches_clean_state(fixture_http_server, monkeypatch):
    with fixture_http_server("rss_valid.xml") as server:
        target = endpoint(source("crash"), server)
        original = ingest_module.process_raw_article

        def crash(*_args, **_kwargs):
            raise SimulatedWorkerCrash

        monkeypatch.setattr(ingest_module, "process_raw_article", crash)
        with pytest.raises(SimulatedWorkerCrash):
            ingest(target)
        crashed = IngestionRun.objects.get()
        assert crashed.status == "RUNNING" and crashed.finished_at is None
        assert (
            RawArticle.objects.count(),
            RawArticle.objects.filter(status="PENDING").count(),
        ) == (10, 10)
        IngestionRun.objects.filter(pk=crashed.pk).update(
            started_at=timezone.now() - timedelta(seconds=settings.NEWS_STALE_RUNNING_SECONDS + 1)
        )
        assert list(stale_running_runs().values_list("pk", flat=True)) == [crashed.pk]
        monkeypatch.setattr(ingest_module, "process_raw_article", original)
        recovered = ingest(target)
        assert recovered.status == "SUCCEEDED" and recovered.raw_unchanged == 10
        assert recovered.items_processed == 10
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (10, 10, 2)
        assert list(IngestionRun.objects.order_by("pk").values_list("status", flat=True)) == [
            "RUNNING",
            "SUCCEEDED",
        ]
        assert RawArticle.objects.filter(status="PENDING").count() == 0
        recovered_state = state_signature()

        Article.objects.all().delete()
        RawArticle.objects.all().delete()
        IngestionRun.objects.all().delete()
        clean = ingest(target)
        assert clean.status == "SUCCEEDED" and state_signature() == recovered_state


@pytest.mark.django_db(transaction=True)
def test_reconciliation_selects_old_pending_and_preserves_receipt(fixture_http_server, monkeypatch):
    with fixture_http_server("rss_changed_item_v1.xml") as server:
        target = endpoint(source("reconcile"), server)
        original = ingest_module.process_raw_article

        def crash(*_args, **_kwargs):
            raise SimulatedWorkerCrash

        monkeypatch.setattr(ingest_module, "process_raw_article", crash)
        with pytest.raises(SimulatedWorkerCrash):
            ingest(target)
        raw = RawArticle.objects.get()
        before = tuple(
            getattr(raw, field)
            for field in (
                "endpoint_id",
                "ingestion_run_id",
                "payload",
                "payload_hash",
                "fetched_at",
                "external_key",
            )
        )
        RawArticle.objects.filter(pk=raw.pk).update(
            created_at=timezone.now() - timedelta(seconds=PENDING_RECONCILE_AFTER_SECONDS + 1)
        )
        selected = []
        monkeypatch.setattr("news.tasks.process_raw_article.delay", selected.append)
        assert reconcile_pending_raw_articles.apply().result == {"dispatched": 1}
        assert selected == [raw.pk]
        outcome = original(selected[0])
        raw.refresh_from_db()
        assert outcome.state is ProcessState.PROCESSED and Article.objects.count() == 1
        assert before == tuple(
            getattr(raw, field)
            for field in (
                "endpoint_id",
                "ingestion_run_id",
                "payload",
                "payload_hash",
                "fetched_at",
                "external_key",
            )
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_intake_matches_single_run(fixture_http_server, monkeypatch):
    with fixture_http_server("rss_valid.xml") as server:
        target = endpoint(source("concurrent"), server)
        ingest(target)
        baseline = state_signature()
        assert (RawArticle.objects.count(), Article.objects.count()) == (10, 10)
        Article.objects.all().delete()
        RawArticle.objects.all().delete()
        IngestionRun.objects.all().delete()

        barrier = threading.Barrier(2, timeout=30)
        adapter_for = ingest_module.adapter_for

        class BarrierAdapter:
            def __init__(self, adapter):
                self.adapter = adapter

            def fetch(self, request, fetcher):
                result = self.adapter.fetch(request, fetcher)
                barrier.wait()
                return result

        monkeypatch.setattr(
            ingest_module, "adapter_for", lambda kind: BarrierAdapter(adapter_for(kind))
        )

        def worker():
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                return ingest(target), pid
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=60) for future in futures]
        assert len({pid for _, pid in results}) == 2
        assert (
            RawArticle.objects.count(),
            Article.objects.count(),
            IngestionRun.objects.count(),
        ) == (10, 10, 2)
        assert state_signature() == baseline
        assert all(summary.status in {"SUCCEEDED", "NO_CHANGE"} for summary, _ in results)
