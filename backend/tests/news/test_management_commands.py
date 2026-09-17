"""Operator command behavior (#19): the manage.py surface for News ingestion."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from news.domain.fingerprints import payload_hash
from news.models import Article, IngestionRun, RawArticle, Source, SourceEndpoint


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


def run(command, *args, **options):
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


@pytest.fixture
def source(db):
    return Source.objects.create(slug="publisher", name="Publisher")


@pytest.fixture
def endpoint(source):
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


# --- news_source -------------------------------------------------------------


@pytest.mark.django_db
def test_source_add_creates_one_active_source():
    output = run(
        "news_source",
        "add",
        "--slug",
        "example-news",
        "--name",
        "Example News",
        "--homepage-url",
        "https://example.com",
        "--default-language",
        "pt-br",
    )

    created = Source.objects.get(slug="example-news")
    assert created.name == "Example News" and created.is_active is True
    assert created.default_language == "pt-br"
    assert f"Created Source {created.pk} example-news" in output


@pytest.mark.django_db
def test_source_add_rejects_a_duplicate_slug(source):
    with pytest.raises(CommandError, match="already exists"):
        run("news_source", "add", "--slug", source.slug, "--name", "Other")

    assert Source.objects.count() == 1


@pytest.mark.django_db
def test_source_add_rejects_an_invalid_slug_or_homepage():
    with pytest.raises(CommandError, match="Invalid Source"):
        run("news_source", "add", "--slug", "not a slug", "--name", "Bad")
    with pytest.raises(CommandError, match="Invalid Source"):
        run("news_source", "add", "--slug", "ok", "--name", "Bad", "--homepage-url", "nope")

    assert Source.objects.count() == 0


@pytest.mark.django_db
def test_source_list_reports_state(source):
    Source.objects.create(slug="second", name="Second", is_active=False)

    output = run("news_source", "list")

    assert "publisher" in output and "second" in output
    assert "SLUG" in output and "ACTIVE" in output


@pytest.mark.django_db
def test_source_list_is_explicit_when_empty(db):
    assert "No Sources configured." in run("news_source", "list")


@pytest.mark.django_db
def test_source_disable_and_enable_only_touch_the_flag(source):
    before = Source.objects.values("slug", "name", "homepage_url", "default_language").get()

    disabled = run("news_source", "disable", source.slug)
    source.refresh_from_db()
    assert source.is_active is False and "Disabled Source" in disabled

    # Repeating is safe and says so rather than pretending to act.
    assert "already disabled" in run("news_source", "disable", source.slug)

    enabled = run("news_source", "enable", source.slug)
    source.refresh_from_db()
    assert source.is_active is True and "Enabled Source" in enabled
    assert Source.objects.values("slug", "name", "homepage_url", "default_language").get() == before


@pytest.mark.django_db
def test_source_actions_reject_an_unknown_slug(db):
    with pytest.raises(CommandError, match="No Source with slug"):
        run("news_source", "disable", "missing")


# --- news_endpoint -----------------------------------------------------------


@pytest.mark.django_db
def test_endpoint_add_uses_model_validation(source):
    output = run(
        "news_endpoint",
        "add",
        "--source",
        source.slug,
        "--kind",
        "RSS",
        "--url",
        "https://example.com/feed.xml",
        "--interval",
        "600",
    )

    created = SourceEndpoint.objects.get()
    assert created.source_id == source.pk and created.kind == "RSS"
    assert created.fetch_interval_seconds == 600 and created.adapter_config == {}
    assert f"Created SourceEndpoint {created.pk}" in output


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com/feed", "http or https"),
        ("not-a-url", "http or https"),
    ],
)
def test_endpoint_add_rejects_an_unusable_url(source, url, message):
    with pytest.raises(CommandError, match="Invalid endpoint"):
        run("news_endpoint", "add", "--source", source.slug, "--kind", "RSS", "--url", url)

    assert SourceEndpoint.objects.count() == 0


@pytest.mark.django_db
def test_endpoint_add_refuses_secret_like_adapter_config(source):
    with pytest.raises(CommandError, match="Secret-like configuration key"):
        run(
            "news_endpoint",
            "add",
            "--source",
            source.slug,
            "--kind",
            "RSS",
            "--url",
            "https://example.com/feed.xml",
            "--adapter-config",
            '{"api_token": "s3cret"}',
        )

    assert SourceEndpoint.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("config", ["not json", '["a"]', "42"])
def test_endpoint_add_requires_a_json_object_config(source, config):
    with pytest.raises(CommandError, match="--adapter-config must be"):
        run(
            "news_endpoint",
            "add",
            "--source",
            source.slug,
            "--kind",
            "RSS",
            "--url",
            "https://example.com/feed.xml",
            "--adapter-config",
            config,
        )


@pytest.mark.django_db
def test_endpoint_add_rejects_a_nonpositive_interval(source):
    with pytest.raises(CommandError, match="Invalid endpoint"):
        run(
            "news_endpoint",
            "add",
            "--source",
            source.slug,
            "--kind",
            "RSS",
            "--url",
            "https://example.com/feed.xml",
            "--interval",
            "0",
        )


@pytest.mark.django_db
def test_endpoint_add_rejects_an_unknown_source(db):
    with pytest.raises(CommandError, match="No Source with slug"):
        run(
            "news_endpoint",
            "add",
            "--source",
            "missing",
            "--kind",
            "RSS",
            "--url",
            "https://example.com/feed.xml",
        )


@pytest.mark.django_db
def test_endpoint_list_can_be_limited_to_one_source(endpoint):
    other = Source.objects.create(slug="other", name="Other")
    SourceEndpoint.objects.create(
        source=other, kind=SourceEndpoint.Kind.JSON_FEED, url="https://other.example/feed.json"
    )

    everything = run("news_endpoint", "list")
    filtered = run("news_endpoint", "list", "--source", "other")

    assert "https://feed.example/rss" in everything and "other.example" in everything
    assert "https://feed.example/rss" not in filtered and "other.example" in filtered


@pytest.mark.django_db
def test_endpoint_can_be_addressed_by_id_or_exact_url(endpoint):
    assert f"SourceEndpoint {endpoint.pk}" in run("news_endpoint", "disable", str(endpoint.pk))
    assert "already disabled" in run("news_endpoint", "disable", endpoint.url)


@pytest.mark.django_db
@pytest.mark.parametrize("identifier", ["999999", "https://unknown.example/rss", "feed.example"])
def test_endpoint_actions_reject_unknown_identifiers(endpoint, identifier):
    with pytest.raises(CommandError, match="No SourceEndpoint"):
        run("news_endpoint", "enable", identifier)


@pytest.mark.django_db
def test_endpoint_disable_does_not_revalidate_the_url(endpoint, monkeypatch):
    """A flag flip must not resolve DNS or re-apply the target policy."""

    def unreachable(*_args, **_kwargs):
        raise AssertionError("enable/disable must not resolve the endpoint URL")

    monkeypatch.setattr("news.adapters.targets._resolve", unreachable)
    before = SourceEndpoint.objects.values("url", "kind", "source_id", "adapter_config").get()

    run("news_endpoint", "disable", str(endpoint.pk))
    run("news_endpoint", "enable", str(endpoint.pk))

    endpoint.refresh_from_db()
    assert endpoint.is_active is True
    assert SourceEndpoint.objects.values("url", "kind", "source_id", "adapter_config").get() == (
        before
    )


# --- news_ingest -------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sync_ingest_against_a_real_loopback_server(source, fixture_http_server):
    """No MockTransport: the hardened fetcher really talks HTTP to 127.0.0.1."""

    with fixture_http_server("rss_valid.xml") as server:
        endpoint = SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.RSS, url=server.url
        )

        output = run("news_ingest", "--endpoint", str(endpoint.pk))

        assert server.requests == 1

    run_row = IngestionRun.objects.get()
    assert run_row.trigger == "MANUAL"
    assert run_row.status == IngestionRun.Status.SUCCEEDED
    assert run_row.items_received == 10 and run_row.raw_created == 10
    assert Article.objects.count() == 10
    assert f"run_id={run_row.pk}" in output
    assert "status=SUCCEEDED" in output
    assert "items_received=10" in output and "raw_created=10" in output
    assert "items_processed=10" in output
    assert "identity_duplicates=0" in output and "content_duplicates=0" in output


@pytest.mark.django_db(transaction=True)
def test_sync_ingest_accepts_the_endpoint_url_as_identifier(source, fixture_http_server):
    with fixture_http_server("jsonfeed_valid.json", content_type="application/feed+json") as server:
        SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.JSON_FEED, url=server.url
        )

        output = run("news_ingest", "--endpoint", server.url)

    assert "status=" in output and IngestionRun.objects.get().trigger == "MANUAL"
    assert Article.objects.exists()


@pytest.mark.django_db
def test_async_ingest_dispatches_the_task_as_manual(endpoint, monkeypatch):
    calls = []

    class Result:
        id = "queued-task-1"

    monkeypatch.setattr(
        "news.tasks.ingest_endpoint.delay",
        lambda endpoint_id, **kwargs: calls.append((endpoint_id, kwargs)) or Result(),
    )

    output = run("news_ingest", "--endpoint", str(endpoint.pk), "--async")

    assert calls == [(endpoint.pk, {"trigger": "MANUAL"})]
    assert "task_id=queued-task-1" in output
    # Nothing ran in this process: no run row was created here.
    assert IngestionRun.objects.count() == 0


@pytest.mark.django_db
def test_ingest_rejects_an_unknown_endpoint(db):
    with pytest.raises(CommandError, match="No SourceEndpoint"):
        run("news_ingest", "--endpoint", "4242")


# --- news_reprocess ----------------------------------------------------------


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
def test_reprocess_resets_a_rejected_row_and_processes_it(endpoint):
    raw = make_raw(endpoint, url="")
    RawArticle.objects.filter(pk=raw.pk).update(
        status=RawArticle.Status.REJECTED,
        rejection_reason="MISSING_CANONICAL_URL",
        processed_at=timezone.now(),
    )
    provenance = RawArticle.objects.values(
        "payload", "payload_hash", "endpoint_id", "ingestion_run_id", "fetched_at", "supersedes_id"
    ).get(pk=raw.pk)
    # The operator fixed the delivered URL by replaying a corrected payload.
    RawArticle.objects.filter(pk=raw.pk).update(url="https://news.example/item")

    output = run("news_reprocess", "--raw", str(raw.pk))

    raw.refresh_from_db()
    assert raw.status == RawArticle.Status.REJECTED
    assert raw.rejection_reason == "MISSING_CANONICAL_URL"
    assert "state=REJECTED" in output and "MISSING_CANONICAL_URL" in output
    # Receipt provenance is never rewritten by a reprocess.
    assert (
        RawArticle.objects.values(
            "payload",
            "payload_hash",
            "endpoint_id",
            "ingestion_run_id",
            "fetched_at",
            "supersedes_id",
        ).get(pk=raw.pk)
        == provenance
    )


@pytest.mark.django_db
def test_reprocess_clears_stale_result_fields_before_processing(endpoint):
    raw = make_raw(endpoint)
    article = Article.objects.create(
        source=endpoint.source,
        endpoint=endpoint,
        raw_article=raw,
        canonical_url="https://news.example/stale",
        title="Stale",
        language="en",
        content_fingerprint="a" * 64,
        first_seen_at=timezone.now(),
    )
    RawArticle.objects.filter(pk=raw.pk).update(
        status=RawArticle.Status.REJECTED,
        outcome=RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT,
        rejection_reason="SOURCE_IDENTITY_CONFLICT",
        processed_at=timezone.now(),
        article=article,
    )

    output = run("news_reprocess", "--raw", str(raw.pk))

    raw.refresh_from_db()
    assert raw.status == RawArticle.Status.PROCESSED
    assert raw.outcome == RawArticle.Outcome.ARTICLE_CREATED
    assert raw.rejection_reason == ""
    assert raw.article_id is not None and raw.article_id != article.pk
    assert "state=PROCESSED" in output and "outcome=ARTICLE_CREATED" in output


@pytest.mark.django_db
@pytest.mark.parametrize("status", [RawArticle.Status.PENDING, RawArticle.Status.PROCESSED])
def test_reprocess_accepts_only_a_rejected_revision(endpoint, status):
    raw = make_raw(endpoint)
    RawArticle.objects.filter(pk=raw.pk).update(status=status)

    with pytest.raises(CommandError, match="is not REJECTED"):
        run("news_reprocess", "--raw", str(raw.pk))

    raw.refresh_from_db()
    assert raw.status == status


@pytest.mark.django_db
def test_reprocess_rejects_an_unknown_raw_id(db):
    with pytest.raises(CommandError, match="No RawArticle with id"):
        run("news_reprocess", "--raw", "123456")
