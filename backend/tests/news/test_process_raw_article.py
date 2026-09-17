from copy import deepcopy
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from news.application.ingest import ingest_endpoint
from news.application.ports import FetchResponse
from news.application.process import ProcessState, process_raw_article
from news.domain.fingerprints import payload_hash
from news.models import Article, RawArticle, Source, SourceEndpoint

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="publisher", name="Publisher", default_language="")
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


def make_payload(**changes):
    result = {
        "external_id": "item-1",
        "url": "https://news.example/item",
        "title": "Item one",
        "summary_html": "<p>Summary.</p>",
        "content_html": "<p>Body.</p>",
        "published_at": "2026-09-17T12:00:00Z",
        "updated_at": None,
        "authors": ["Writer"],
        "language": "en-US",
        "raw": {},
        "truncated": False,
    }
    result.update(changes)
    return result


def make_raw(endpoint, *, key="item-1", payload=None, fetched_at=None, **changes):
    material = make_payload() if payload is None else payload
    fields = {
        "endpoint": endpoint,
        "external_key_kind": RawArticle.ExternalKeyKind.EXTERNAL_ID,
        "external_key": key,
        "external_id": material.get("external_id") or "",
        "url": material.get("url") or "",
        "payload": material,
        "payload_hash": payload_hash(material),
        "fetched_at": fetched_at or timezone.now(),
    }
    fields.update(changes)
    return RawArticle.objects.create(**fields)


def ingest_fixture(endpoint, monkeypatch, name):
    body = (FIXTURES / name).read_bytes()

    class FixtureFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, *_args, **_kwargs):
            return FetchResponse(body=body, content_type="application/rss+xml")

    monkeypatch.setattr("news.application.ingest.Fetcher", FixtureFetcher)
    return ingest_endpoint(endpoint.pk, trigger="MANUAL")


@pytest.mark.django_db
def test_rss_valid_creates_normalized_articles(endpoint, monkeypatch):
    summary = ingest_fixture(endpoint, monkeypatch, "rss_valid.xml")
    assert summary.items_processed == 10
    assert (
        RawArticle.objects.filter(
            status=RawArticle.Status.PROCESSED, outcome=RawArticle.Outcome.ARTICLE_CREATED
        ).count()
        == 10
    )
    assert Article.objects.count() == 10
    for article in Article.objects.all():
        assert article.title and article.body_text and "<" not in article.body_text
        assert article.canonical_url.startswith("https://news.example/story/")
        assert article.published_at is not None and article.published_at.utcoffset() is not None
        assert article.language == "en-us"


@pytest.mark.django_db
def test_html_fixture_drops_dangerous_content_and_preserves_paragraphs(endpoint, monkeypatch):
    ingest_fixture(endpoint, monkeypatch, "rss_html_content.xml")
    body = Article.objects.get().body_text
    assert body == "Hello reader.\nSecond paragraph."
    assert all(value not in body for value in ("alert", "danger", "frame text", "hidden comment"))


@pytest.mark.django_db
def test_tracking_urls_share_canonical_identity_without_content_dedup(endpoint, monkeypatch):
    ingest_fixture(endpoint, monkeypatch, "rss_tracking_urls.xml")
    assert Article.objects.count() == 1
    article = Article.objects.get()
    assert article.canonical_url == "https://news.example/item?a=1&b=2"
    raws = list(RawArticle.objects.order_by("pk"))
    assert len(raws) == 2
    assert raws[0].outcome == RawArticle.Outcome.ARTICLE_CREATED
    assert raws[1].outcome == RawArticle.Outcome.IDENTITY_DUPLICATE
    assert raws[1].article_id == article.pk


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (make_payload(title=None, summary_html=None, content_html=None), "EMPTY_ENTRY"),
        (make_payload(content_html="x" * 250_000), "BODY_TOO_LARGE"),
        (make_payload(url="", external_id="guid-only"), "MISSING_CANONICAL_URL"),
        (make_payload(url="mailto:editor@example.com"), "MISSING_CANONICAL_URL"),
    ],
)
def test_normalization_rejections_preserve_raw_payload(endpoint, payload, reason, pulso_caplog):
    original = deepcopy(payload)
    raw = make_raw(endpoint, key=reason, payload=payload)
    assert process_raw_article(raw.pk).state == ProcessState.REJECTED
    raw.refresh_from_db()
    assert raw.status == RawArticle.Status.REJECTED
    assert raw.rejection_reason == reason and raw.article_id is None
    assert raw.payload == original and Article.objects.count() == 0
    record = next(
        record
        for record in pulso_caplog.records
        if getattr(record, "rejection_reason", None) == reason
    )
    assert record.raw_article_id == raw.pk and record.external_key == raw.external_key
    assert not any(hasattr(record, field) for field in ("payload", "title", "body_text"))


@pytest.mark.django_db
def test_id_only_rss_raw_is_rejected_but_preserved(endpoint, monkeypatch):
    ingest_fixture(endpoint, monkeypatch, "rss_id_only_entry.xml")
    raw = RawArticle.objects.get()
    original = deepcopy(raw.payload)
    assert raw.external_id == "guid-only-1" and raw.status == RawArticle.Status.REJECTED
    assert raw.rejection_reason == "MISSING_CANONICAL_URL"
    assert raw.payload == original and raw.article_id is None and Article.objects.count() == 0


@pytest.mark.django_db
def test_byline_over_255_persists_and_first_seen_uses_fetched_at(endpoint):
    seen = timezone.now()
    raw = make_raw(
        endpoint,
        fetched_at=seen,
        payload=make_payload(authors=["a" * 300, "b" * 300], published_at=None, language=None),
    )
    process_raw_article(raw.pk)
    article = Article.objects.get()
    assert len(article.byline) == 512 and len(article.byline) > 255
    assert article.published_at is None and article.first_seen_at == seen
    assert article.language == ""


@pytest.mark.django_db
def test_changed_revision_updates_in_place_and_preserves_invariants(endpoint, monkeypatch):
    ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v1.xml")
    article = Article.objects.get()
    original_id = article.pk
    original_source = article.source_id
    first_seen = article.first_seen_at
    old_updated = article.updated_at
    ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v2.xml")
    article.refresh_from_db()
    v2 = RawArticle.objects.latest("pk")
    assert article.pk == original_id and article.source_id == original_source
    assert article.first_seen_at == first_seen and article.updated_at > old_updated
    assert (article.title, article.body_text) == ("Version two", "Second body.")
    assert article.raw_article_id == v2.pk
    assert v2.article_id == article.pk
    assert v2.outcome == RawArticle.Outcome.ARTICLE_UPDATED
    assert Article.objects.count() == 1


@pytest.mark.django_db
def test_identity_duplicate_links_raw_without_mutating_article(endpoint):
    first = make_raw(endpoint)
    process_raw_article(first.pk)
    article = Article.objects.get()
    original_updated = article.updated_at
    original_raw_id = article.raw_article_id
    duplicate = make_raw(endpoint, key="second", supersedes=first)
    process_raw_article(duplicate.pk)
    article.refresh_from_db()
    duplicate.refresh_from_db()
    assert duplicate.outcome == RawArticle.Outcome.IDENTITY_DUPLICATE
    assert duplicate.article_id == article.pk
    assert article.updated_at == original_updated and article.raw_article_id == original_raw_id


@pytest.mark.django_db
def test_same_fingerprint_different_urls_remain_separate(endpoint):
    first = make_raw(endpoint)
    second_payload = make_payload(external_id="item-2", url="https://news.example/other")
    second = make_raw(endpoint, key="item-2", payload=second_payload)
    process_raw_article(first.pk)
    process_raw_article(second.pk)
    assert Article.objects.count() == 2
    assert Article.objects.filter(duplicate_of__isnull=False).count() == 0
    assert len(set(Article.objects.values_list("content_fingerprint", flat=True))) == 1


@pytest.mark.django_db
def test_cross_source_canonical_owner_is_unchanged(endpoint):
    first = make_raw(endpoint)
    process_raw_article(first.pk)
    article = Article.objects.get()
    before = {
        field: getattr(article, field)
        for field in (
            "source_id",
            "endpoint_id",
            "raw_article_id",
            "title",
            "body_text",
            "content_fingerprint",
            "updated_at",
        )
    }
    other_source = Source.objects.create(slug="other", name="Other")
    other_endpoint = SourceEndpoint.objects.create(
        source=other_source, kind="RSS", url="https://other.example/feed"
    )
    conflict = make_raw(
        other_endpoint,
        key="other",
        payload=make_payload(external_id="other", title="Other title"),
    )
    process_raw_article(conflict.pk)
    conflict.refresh_from_db()
    article.refresh_from_db()
    assert conflict.status == RawArticle.Status.REJECTED
    assert conflict.rejection_reason == "SOURCE_IDENTITY_CONFLICT"
    assert Article.objects.count() == 1
    assert before == {field: getattr(article, field) for field in before}


@pytest.mark.django_db
def test_same_source_conflicting_candidates_are_rejected(endpoint):
    first = make_raw(
        endpoint, key="a", payload=make_payload(external_id="a", url="https://news.example/a")
    )
    second = make_raw(
        endpoint, key="b", payload=make_payload(external_id="b", url="https://news.example/b")
    )
    process_raw_article(first.pk)
    process_raw_article(second.pk)
    before = list(Article.objects.order_by("pk").values())
    conflict = make_raw(
        endpoint,
        key="conflict",
        payload=make_payload(external_id="a", url="https://news.example/b", title="Conflict"),
    )
    process_raw_article(conflict.pk)
    conflict.refresh_from_db()
    assert conflict.status == RawArticle.Status.REJECTED
    assert conflict.rejection_reason == "IDENTITY_CONFLICT"
    assert before == list(Article.objects.order_by("pk").values())


@pytest.mark.django_db
@pytest.mark.parametrize("status", [RawArticle.Status.PROCESSED, RawArticle.Status.REJECTED])
def test_finalized_rows_return_without_writes(endpoint, status):
    raw = make_raw(endpoint, status=status, processed_at=timezone.now())
    with CaptureQueriesContext(connection) as captured:
        outcome = process_raw_article(raw.pk)
    assert outcome.state == ProcessState.SKIPPED
    writes = [
        query["sql"]
        for query in captured
        if query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
    ]
    assert writes == []


@pytest.mark.django_db
def test_unavailable_row_keeps_explicit_locked_result():
    assert process_raw_article(999_999).state == ProcessState.LOCKED
