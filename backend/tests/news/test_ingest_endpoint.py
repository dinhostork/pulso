import json
from dataclasses import replace

import pytest
from django.conf import settings
from django.utils import timezone

from news.application.ingest import ingest_endpoint
from news.application.ports import (
    FetchedItem,
    FetchError,
    FetchErrorKind,
    FetchResponse,
    FetchResult,
    RejectedItem,
)
from news.application.process import ProcessOutcome, ProcessState
from news.domain.fingerprints import payload_hash
from news.models import Article, IngestionRun, RawArticle, Source, SourceEndpoint


class FakeFetcher:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class FakeAdapter:
    kind = "RSS"

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def fetch(self, request, fetcher):
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))
    monkeypatch.setattr("news.application.ingest.Fetcher", FakeFetcher)


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="intake", name="Intake")
    return SourceEndpoint.objects.create(source=source, kind="RSS", url="https://feed.example/rss")


def item(identity="one", **changes):
    fields = dict(external_id=identity, url="https://news.example/one?utm_source=x", title="One")
    fields.update(changes)
    return FetchedItem(**fields)


def run(endpoint, monkeypatch, result):
    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: FakeAdapter(result=result))
    return ingest_endpoint(endpoint.pk, trigger="MANUAL")


@pytest.mark.django_db
def test_revisions_identity_rejections_and_duplicate(endpoint, monkeypatch):
    first = item()
    summary = run(endpoint, monkeypatch, FetchResult(items=(first, first)))
    assert (summary.raw_created, summary.raw_unchanged, summary.status) == (1, 1, "SUCCEEDED")
    original = RawArticle.objects.get()
    assert run(endpoint, monkeypatch, FetchResult(items=(first,))).status == "NO_CHANGE"
    summary = run(endpoint, monkeypatch, FetchResult(items=(replace(first, title="Changed"),)))
    assert summary.raw_changed == 1 and RawArticle.objects.latest("pk").supersedes_id == original.pk
    result = FetchResult(
        items=(
            item("guid", url=None),
            item(None, url="https://NEWS.example/x?utm_source=y"),
            item(None, url="mailto:x@y"),
        ),
        rejected=(RejectedItem(9, "BAD"),),
    )
    summary = run(endpoint, monkeypatch, result)
    assert summary.items_rejected == 2 and summary.status == "PARTIAL"
    guid = RawArticle.objects.get(external_id="guid")
    url = RawArticle.objects.get(external_key_kind="CANONICAL_URL")
    assert (
        guid.url == "" and url.external_key == "https://news.example/x" and "utm_source" in url.url
    )


@pytest.mark.django_db
def test_fetch_errors_and_304(endpoint, monkeypatch):
    for error, retry, status in (
        (FetchError(FetchErrorKind.TIMEOUT, "Safe", retryable=True), True, None),
        (FetchError(FetchErrorKind.HTTP_STATUS, "Safe", http_status=404), False, 404),
    ):
        monkeypatch.setattr(
            "news.application.ingest.adapter_for", lambda _, e=error: FakeAdapter(error=e)
        )
        summary = ingest_endpoint(endpoint.pk, trigger="MANUAL")
        saved = IngestionRun.objects.get(pk=summary.run_id)
        assert (
            saved.status == "FAILED" and saved.will_retry is retry and saved.http_status == status
        )
    summary = run(
        endpoint, monkeypatch, FetchResult(not_modified=True, etag="new", last_modified="today")
    )
    endpoint.refresh_from_db()
    assert summary.status == "NO_CHANGE" and (endpoint.etag, endpoint.last_modified) == (
        "new",
        "today",
    )


@pytest.mark.django_db
def test_payload_and_item_caps(endpoint, monkeypatch, pulso_caplog):
    huge = item(content_html="x" * (settings.NEWS_INGEST_MAX_PAYLOAD_BYTES + 1000))
    run(endpoint, monkeypatch, FetchResult(items=(huge,)))
    raw = RawArticle.objects.get()
    encoded = json.dumps(
        raw.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert (
        len(encoded) <= settings.NEWS_INGEST_MAX_PAYLOAD_BYTES
        and raw.payload["truncated"]
        and raw.payload_hash == payload_hash(raw.payload)
    )
    many = tuple(item(str(i), title=str(i)) for i in range(501))
    summary = run(endpoint, monkeypatch, FetchResult(items=many))
    assert summary.items_received == 501 and summary.items_rejected == 1


@pytest.mark.django_db
def test_processing_failure_and_replay(endpoint, monkeypatch):
    old = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind="EXTERNAL_ID",
        external_key="old",
        external_id="old",
        url="",
        payload={},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    called = []

    def process(pk, *, logger=None):
        called.append(pk)
        assert logger is not None, "the ingestion loop must pass its run logger"
        if pk == old.pk:
            raise RuntimeError("secret")
        # The loop counts dedup outcomes from this value (#18), so the double
        # must return one rather than None.
        return ProcessOutcome(
            raw_id=pk,
            state=ProcessState.PROCESSED,
            outcome=RawArticle.Outcome.ARTICLE_CREATED,
            article_id=None,
        )

    monkeypatch.setattr("news.application.ingest.process_raw_article", process)
    summary = run(endpoint, monkeypatch, FetchResult(items=(item("new"),)))
    assert summary.status == "PARTIAL" and summary.items_failed == 1 and old.pk in called
    old.refresh_from_db()
    assert old.ingestion_run_id is None


class FixtureAdapter:
    kind = "RSS"

    def __init__(self, body):
        self.body = body

    def fetch(self, request, fetcher):
        from news.adapters.rss import RssAdapter
        from news.application.ports import FetchResponse

        class Port:
            def get(self, *args, **kwargs):
                return FetchResponse(body=self_body, content_type="application/rss+xml")

        self_body = self.body
        return RssAdapter().fetch(request, Port())


def fixture(name):
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "fixtures" / "news" / name).read_bytes()


def fixture_run(endpoint, monkeypatch, name):
    monkeypatch.setattr(
        "news.application.ingest.adapter_for", lambda _: FixtureAdapter(fixture(name))
    )
    return ingest_endpoint(endpoint.pk, trigger="MANUAL")


def clear_processed_publications():
    Article.objects.all().delete()
    RawArticle.objects.all().delete()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "fixture_name", "content_type"),
    (
        ("RSS", "rss_valid.xml", "application/rss+xml"),
        ("JSON_FEED", "jsonfeed_valid.json", "application/feed+json"),
    ),
)
def test_intake_boundary_is_independent_of_adapter_fields(
    endpoint, monkeypatch, kind, fixture_name, content_type
):
    body = fixture(fixture_name)

    class AdapterFixtureFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, *_args, **_kwargs):
            return FetchResponse(body=body, content_type=content_type)

    endpoint.kind = kind
    endpoint.save(update_fields=["kind"])
    monkeypatch.setattr("news.application.ingest.Fetcher", AdapterFixtureFetcher)

    summary = ingest_endpoint(endpoint.pk, trigger="MANUAL")

    assert summary.raw_created > 0
    assert summary.raw_created + summary.raw_unchanged + summary.raw_changed > 0
    assert all(
        set(raw.payload)
        <= {
            "external_id",
            "url",
            "title",
            "summary_html",
            "content_html",
            "published_at",
            "updated_at",
            "authors",
            "language",
            "raw",
            "truncated",
        }
        for raw in RawArticle.objects.all()
    )


@pytest.mark.django_db
def test_rss_fixture_idempotency_duplicate_and_malformed(endpoint, monkeypatch, pulso_caplog):
    first = fixture_run(endpoint, monkeypatch, "rss_valid.xml")
    second = fixture_run(endpoint, monkeypatch, "rss_valid.xml")
    assert first.raw_created == 10 and first.status == "SUCCEEDED"
    assert second.raw_unchanged == 10 and second.status == "NO_CHANGE"
    clear_processed_publications()
    duplicate = fixture_run(endpoint, monkeypatch, "rss_duplicate_entry.xml")
    assert (
        duplicate.raw_created == 1
        and duplicate.raw_unchanged == 1
        and RawArticle.objects.count() == 1
    )
    clear_processed_publications()
    malformed = fixture_run(endpoint, monkeypatch, "rss_malformed_item.xml")
    assert (
        malformed.status == "PARTIAL"
        and malformed.items_rejected == 1
        and RawArticle.objects.count() == 2
    )
    assert any(
        getattr(record, "rejection_reason", None) == "MISSING_IDENTITY"
        for record in pulso_caplog.records
    )


@pytest.mark.django_db
def test_rss_id_only_fixture_and_changed_revision(endpoint, monkeypatch):
    summary = fixture_run(endpoint, monkeypatch, "rss_id_only_entry.xml")
    assert summary.status == "PARTIAL" and summary.items_rejected == 1
    raw = RawArticle.objects.get()
    assert raw.external_key == "guid-only-1" and raw.url == ""
    clear_processed_publications()
    one = item("revision", title="v1")
    run(endpoint, monkeypatch, FetchResult(items=(one,)))
    original = RawArticle.objects.get()
    changed = run(endpoint, monkeypatch, FetchResult(items=(replace(one, title="v2"),)))
    assert changed.raw_changed == 1 and RawArticle.objects.latest("pk").supersedes_id == original.pk


@pytest.mark.django_db
def test_failed_run_and_processing_logs_hide_sentinels(endpoint, monkeypatch, pulso_caplog):
    sentinel = "SECRET_HEADER_BODY_CONFIG_PAYLOAD"
    SourceEndpoint.objects.filter(pk=endpoint.pk).update(
        adapter_config={"credential_env": sentinel}
    )
    error = FetchError(FetchErrorKind.TIMEOUT, "Safe timeout", retryable=True)
    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: FakeAdapter(error=error))
    ingest_endpoint(endpoint.pk, trigger="MANUAL")
    assert (
        sentinel not in pulso_caplog.text
        and sentinel not in IngestionRun.objects.latest("pk").error_message
    )
