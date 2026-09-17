"""Deduplication behavior end to end against PostgreSQL (ADR-0010).

These tests drive the real `ingest_endpoint` and `process_raw_article` so the
database uniqueness rules, the candidate lookups and the pure decision are all
exercised together.
"""

from pathlib import Path

import pytest
from django.utils import timezone

from news.application.ingest import ingest_endpoint
from news.application.ports import FetchedItem, FetchResponse, FetchResult
from news.application.registry import adapter_for
from news.domain.dedup import Decision, DecisionKind
from news.domain.fingerprints import fingerprint_input_length
from news.models import Article, RawArticle, Source, SourceEndpoint

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"

# One wire report republished under several URLs; long enough for Tier 2.
WIRE_TITLE = "Regulator clears the merger with conditions"
WIRE_BODY = (
    "<p>The competition regulator cleared the merger on Thursday subject to the disposal of "
    "two regional depots and a five-year pricing commitment.</p><p>The parties said they "
    "expected to complete the transaction before the end of the financial year.</p>"
)

# Publication fields whose value must never change for an Article that another
# delivery merely referenced.
PROVENANCE_FIELDS = (
    "source_id",
    "endpoint_id",
    "raw_article_id",
    "updated_at",
    "canonical_url",
    "external_id",
    "title",
    "description",
    "body_text",
    "content_fingerprint",
    "duplicate_of_id",
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


def make_source(slug, **changes):
    return Source.objects.create(slug=slug, name=slug.title(), **changes)


def make_endpoint(source, url):
    return SourceEndpoint.objects.create(source=source, kind=SourceEndpoint.Kind.RSS, url=url)


@pytest.fixture
def publisher(db):
    return make_source("publisher")


@pytest.fixture
def endpoint(publisher):
    return make_endpoint(publisher, "https://feed.example/rss")


def run_fixture(endpoint, monkeypatch, name):
    """Ingest a fixture document through the real RSS adapter."""

    body = (FIXTURES / name).read_bytes()

    class FixtureFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, *_args, **_kwargs):
            return FetchResponse(body=body, content_type="application/rss+xml")

    monkeypatch.setattr("news.application.ingest.Fetcher", FixtureFetcher)
    # Restore the real registry: an earlier `run_items` call in the same test
    # may have replaced it with a stub adapter.
    monkeypatch.setattr("news.application.ingest.adapter_for", adapter_for)
    return ingest_endpoint(endpoint.pk, trigger="MANUAL")


def run_items(endpoint, monkeypatch, *items):
    """Ingest adapter-shaped items; only the transport/parse step is faked."""

    class StubAdapter:
        kind = endpoint.kind

        def fetch(self, request, fetcher):
            return FetchResult(items=tuple(items))

    class NullFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("news.application.ingest.Fetcher", NullFetcher)
    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: StubAdapter())
    return ingest_endpoint(endpoint.pk, trigger="MANUAL")


def wire_item(external_id, url):
    return FetchedItem(external_id=external_id, url=url, title=WIRE_TITLE, content_html=WIRE_BODY)


def snapshot(article):
    article.refresh_from_db()
    return {field: getattr(article, field) for field in PROVENANCE_FIELDS}


def test_same_external_id_new_url_updates_existing_article(endpoint, monkeypatch):
    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    article = Article.objects.get(canonical_url="https://news.example/story/1")
    original = (article.pk, article.source_id, article.first_seen_at, article.created_at)

    summary = run_fixture(endpoint, monkeypatch, "rss_same_external_id_new_url.xml")

    article.refresh_from_db()
    revision = RawArticle.objects.latest("pk")
    assert Article.objects.count() == 10
    assert (article.pk, article.source_id, article.first_seen_at, article.created_at) == original
    assert article.canonical_url == "https://news.example/story/1-updated"
    assert article.external_id == "story-1"
    assert article.raw_article_id == revision.pk
    assert revision.outcome == RawArticle.Outcome.ARTICLE_UPDATED
    assert revision.article_id == article.pk
    assert (summary.identity_duplicates, summary.content_duplicates) == (0, 0)


def test_same_external_id_new_url_against_same_source_url_is_identity_conflict(
    endpoint, monkeypatch
):
    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    run_items(
        endpoint,
        monkeypatch,
        FetchedItem(
            external_id="owner-1",
            url="https://news.example/story/1-updated",
            title="Owner of the moved link",
            content_html="<p>Owner body.</p>",
        ),
    )
    by_external_id = snapshot(Article.objects.get(external_id="story-1"))
    by_url = snapshot(Article.objects.get(canonical_url="https://news.example/story/1-updated"))
    count = Article.objects.count()

    summary = run_fixture(endpoint, monkeypatch, "rss_same_external_id_new_url.xml")

    rejected = RawArticle.objects.latest("pk")
    assert rejected.status == RawArticle.Status.REJECTED
    assert rejected.outcome == RawArticle.Outcome.IDENTITY_CONFLICT
    assert rejected.rejection_reason == RawArticle.Outcome.IDENTITY_CONFLICT
    assert Article.objects.count() == count
    assert snapshot(Article.objects.get(external_id="story-1")) == by_external_id
    assert (
        snapshot(Article.objects.get(canonical_url="https://news.example/story/1-updated"))
        == by_url
    )
    assert summary.status == "PARTIAL" and summary.raw_rejected == 1
    assert summary.source_identity_conflicts == 0


def test_same_external_id_new_url_against_other_source_url_is_source_conflict(
    endpoint, monkeypatch
):
    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    other = make_endpoint(make_source("owner"), "https://owner.example/rss")
    run_items(
        other,
        monkeypatch,
        FetchedItem(
            external_id="owner-1",
            url="https://news.example/story/1-updated",
            title="Owner of the moved link",
            content_html="<p>Owner body.</p>",
        ),
    )
    owner_article = Article.objects.get(canonical_url="https://news.example/story/1-updated")
    before = snapshot(owner_article)
    mine = snapshot(Article.objects.get(external_id="story-1"))
    count = Article.objects.count()

    summary = run_fixture(endpoint, monkeypatch, "rss_same_external_id_new_url.xml")

    rejected = RawArticle.objects.filter(endpoint=endpoint).latest("pk")
    assert rejected.status == RawArticle.Status.REJECTED
    assert rejected.outcome == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    assert rejected.rejection_reason == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    assert rejected.article_id == owner_article.pk
    assert Article.objects.count() == count
    assert snapshot(owner_article) == before
    assert snapshot(Article.objects.get(external_id="story-1")) == mine
    assert summary.source_identity_conflicts == 1


def test_same_canonical_other_endpoint_is_identity_duplicate(endpoint, publisher, monkeypatch):
    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    first = snapshot(Article.objects.get(canonical_url="https://news.example/story/1"))
    second = snapshot(Article.objects.get(canonical_url="https://news.example/story/2"))
    mirror = make_endpoint(publisher, "https://feed.example/mirror")

    summary = run_fixture(mirror, monkeypatch, "rss_same_canonical_other_endpoint.xml")

    mirrored = list(RawArticle.objects.filter(endpoint=mirror).order_by("pk"))
    assert len(mirrored) == 2
    assert all(raw.status == RawArticle.Status.PROCESSED for raw in mirrored)
    assert all(raw.outcome == RawArticle.Outcome.IDENTITY_DUPLICATE for raw in mirrored)
    assert Article.objects.count() == 10
    assert [raw.article.canonical_url for raw in mirrored] == [
        "https://news.example/story/1",
        "https://news.example/story/2",
    ]
    # The existing publications keep their own endpoint, revision and timestamps.
    assert snapshot(Article.objects.get(canonical_url="https://news.example/story/1")) == first
    assert snapshot(Article.objects.get(canonical_url="https://news.example/story/2")) == second
    assert summary.identity_duplicates == 2 and summary.raw_rejected == 0


def test_cross_source_canonical_url_rejects_the_delivery(endpoint, monkeypatch, pulso_caplog):
    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    owned = {
        url: snapshot(Article.objects.get(canonical_url=url))
        for url in ("https://news.example/story/1", "https://news.example/story/2")
    }
    aggregator = make_source("aggregator")
    other = make_endpoint(aggregator, "https://aggregator.example/rss")

    pulso_caplog.clear()
    summary = run_fixture(other, monkeypatch, "rss_same_canonical_other_source.xml")

    rejected = list(RawArticle.objects.filter(endpoint=other).order_by("pk"))
    assert len(rejected) == 2
    for raw in rejected:
        assert raw.status == RawArticle.Status.REJECTED
        assert raw.outcome == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
        assert raw.rejection_reason == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
        assert raw.article_id is not None
    assert Article.objects.count() == 10
    assert {raw.article.canonical_url for raw in rejected} == set(owned)
    for url, before in owned.items():
        assert snapshot(Article.objects.get(canonical_url=url)) == before
    assert summary.status == "PARTIAL"
    assert summary.source_identity_conflicts == summary.raw_rejected == 2

    warnings = [
        record
        for record in pulso_caplog.records
        if getattr(record, "existing_source_slug", None) is not None
    ]
    assert len(warnings) == 2
    for record in warnings:
        assert record.incoming_source_slug == "aggregator"
        assert record.existing_source_slug == "publisher"
        assert record.canonical_url in owned
        assert record.incoming_endpoint_id == other.pk
        assert record.existing_endpoint_id == endpoint.pk
        assert not any(
            hasattr(record, field)
            for field in ("title", "description", "body_text", "payload", "summary_html")
        )


def test_source_conflict_never_reattributes_the_article(endpoint, monkeypatch):
    """The winning publication keeps its Source even after repeated deliveries."""

    run_fixture(endpoint, monkeypatch, "rss_valid.xml")
    before = snapshot(Article.objects.get(canonical_url="https://news.example/story/1"))
    other = make_endpoint(make_source("aggregator"), "https://aggregator.example/rss")

    run_fixture(other, monkeypatch, "rss_same_canonical_other_source.xml")
    run_fixture(other, monkeypatch, "rss_same_canonical_other_source.xml")

    assert Article.objects.filter(source__slug="aggregator").count() == 0
    assert snapshot(Article.objects.get(canonical_url="https://news.example/story/1")) == before


def test_syndicated_copy_links_duplicate_of(publisher, monkeypatch):
    origin = make_endpoint(make_source("origin"), "https://origin.example/rss")
    run_fixture(origin, monkeypatch, "rss_syndication_origin.xml")
    original = Article.objects.get()
    syndicate = make_endpoint(make_source("syndicate"), "https://syndicate.example/rss")

    summary = run_fixture(syndicate, monkeypatch, "rss_syndicated_copy.xml")

    copy = Article.objects.exclude(pk=original.pk).get()
    original.refresh_from_db()
    assert Article.objects.count() == 2
    assert copy.duplicate_of_id == original.pk and original.duplicate_of_id is None
    assert copy.source.slug == "syndicate" and original.source.slug == "origin"
    assert copy.canonical_url == "https://syndicate.example/wire/central-bank-holds-rate"
    assert original.canonical_url == "https://origin.example/report/central-bank-holds-rate"
    assert copy.content_fingerprint == original.content_fingerprint
    raw = RawArticle.objects.get(endpoint=syndicate)
    assert raw.status == RawArticle.Status.PROCESSED
    assert raw.outcome == RawArticle.Outcome.CONTENT_DUPLICATE
    assert raw.article_id == copy.pk
    assert RawArticle.objects.filter(status=RawArticle.Status.REJECTED).count() == 0
    assert summary.content_duplicates == 1 and summary.status == "SUCCEEDED"


def test_content_duplicate_links_earliest_article(publisher, monkeypatch):
    first = make_endpoint(make_source("wire-one"), "https://one.example/rss")
    second = make_endpoint(make_source("wire-two"), "https://two.example/rss")
    third = make_endpoint(make_source("wire-three"), "https://three.example/rss")
    run_items(first, monkeypatch, wire_item("a", "https://one.example/merger"))
    run_items(second, monkeypatch, wire_item("b", "https://two.example/merger"))
    older, newer = Article.objects.order_by("pk")
    # Make the higher primary key the earliest row, so only `created_at` ordering
    # can select it; database insertion order must not decide.
    Article.objects.filter(pk=older.pk).update(created_at=newer.created_at + timezone.timedelta(1))

    run_items(third, monkeypatch, wire_item("c", "https://three.example/merger"))

    latest = Article.objects.order_by("-pk").first()
    assert newer.pk > older.pk
    assert latest.duplicate_of_id == newer.pk
    assert Article.objects.count() == 3


def test_short_identical_titles_are_not_content_duplicates(endpoint, monkeypatch):
    other = make_endpoint(make_source("second"), "https://second.example/rss")
    assert fingerprint_input_length("Market update", "", "") < 200
    run_items(
        endpoint,
        monkeypatch,
        FetchedItem(external_id="one", url="https://news.example/short", title="Market update"),
    )
    run_items(
        other,
        monkeypatch,
        FetchedItem(external_id="two", url="https://second.example/short", title="Market update"),
    )

    articles = list(Article.objects.order_by("pk"))
    assert len(articles) == 2
    assert all(article.duplicate_of_id is None for article in articles)
    assert articles[0].content_fingerprint == articles[1].content_fingerprint
    assert RawArticle.objects.filter(status=RawArticle.Status.REJECTED).count() == 0


def test_same_story_publications_stay_separate_articles(endpoint, monkeypatch):
    summary = run_fixture(endpoint, monkeypatch, "rss_same_story_different_articles.xml")

    articles = list(Article.objects.all())
    assert len(articles) == 3
    assert all(article.duplicate_of_id is None for article in articles)
    assert len({article.content_fingerprint for article in articles}) == 3
    assert RawArticle.objects.filter(status=RawArticle.Status.REJECTED).count() == 0
    assert (summary.identity_duplicates, summary.content_duplicates) == (0, 0)
    assert summary.status == "SUCCEEDED"


def test_identical_payload_creates_no_new_raw_article(endpoint, monkeypatch):
    """#16 idempotency: an unchanged delivery is not a new revision at all."""

    item = wire_item("wire-1", "https://news.example/merger")
    run_items(endpoint, monkeypatch, item)
    summary = run_items(endpoint, monkeypatch, item)

    assert RawArticle.objects.count() == 1 and Article.objects.count() == 1
    assert summary.raw_unchanged == 1 and summary.raw_created == 0
    assert summary.items_processed == 0 and summary.identity_duplicates == 0


def test_distinct_revision_with_unchanged_publication_is_identity_duplicate(endpoint, monkeypatch):
    """#18: a new revision that normalizes to the same publication touches nothing."""

    run_items(
        endpoint, monkeypatch, wire_item("wire-1", "https://news.example/merger?utm_source=a")
    )
    article = Article.objects.get()
    before = snapshot(article)

    summary = run_items(
        endpoint, monkeypatch, wire_item("wire-1", "https://news.example/merger?utm_source=b")
    )

    revision = RawArticle.objects.latest("pk")
    assert RawArticle.objects.count() == 2 and Article.objects.count() == 1
    assert revision.outcome == RawArticle.Outcome.IDENTITY_DUPLICATE
    assert revision.article_id == article.pk
    assert snapshot(article) == before
    assert summary.identity_duplicates == 1


def test_processing_rejection_makes_the_run_partial(endpoint, monkeypatch):
    summary = run_fixture(endpoint, monkeypatch, "rss_id_only_entry.xml")

    assert summary.status == "PARTIAL"
    assert summary.raw_rejected == 1 and summary.source_identity_conflicts == 0
    assert Article.objects.count() == 0


def test_unexplained_integrity_error_is_reraised(endpoint, monkeypatch):
    """A rejected insert that re-decides to CREATE is a defect, not a dedup race."""

    run_items(endpoint, monkeypatch, wire_item("wire-1", "https://news.example/merger"))
    monkeypatch.setattr(
        "news.application.process.decide",
        lambda *_args, **_kwargs: Decision(DecisionKind.CREATE),
    )

    summary = run_items(
        endpoint, monkeypatch, wire_item("wire-2", "https://news.example/merger?utm_source=x")
    )

    assert summary.items_failed == 1 and summary.status == "PARTIAL"
    assert Article.objects.count() == 1
    assert RawArticle.objects.get(external_id="wire-2").status == RawArticle.Status.PENDING
