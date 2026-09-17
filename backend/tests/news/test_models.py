"""Verify News provenance, identity and endpoint validation contracts."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils import timezone

from news.models import Article, RawArticle, Source, SourceEndpoint


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    """Model validation resolves names, so existing persistence tests use fake DNS."""

    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


@pytest.fixture
def source(db):
    return Source.objects.create(slug="publisher-one", name="Publisher One")


@pytest.fixture
def endpoint(source):
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://example.com/feed.xml"
    )


def make_raw(endpoint, **overrides):
    fields = {
        "endpoint": endpoint,
        "external_key_kind": RawArticle.ExternalKeyKind.EXTERNAL_ID,
        "external_key": "guid-1",
        "external_id": "guid-1",
        "url": "https://example.com/one",
        "payload": {"title": "One"},
        "payload_hash": "a" * 64,
        "fetched_at": timezone.now(),
    }
    fields.update(overrides)
    return RawArticle.objects.create(**fields)


def make_article(source, endpoint, raw, **overrides):
    fields = {
        "source": source,
        "endpoint": endpoint,
        "raw_article": raw,
        "external_id": "guid-1",
        "canonical_url": "https://example.com/one",
        "title": "One",
        "language": "en",
        "content_fingerprint": "a" * 64,
        "first_seen_at": timezone.now(),
    }
    fields.update(overrides)
    return Article.objects.create(**fields)


@pytest.mark.django_db
def test_source_and_endpoint_unique_keys_and_positive_interval(source, endpoint):
    assert endpoint.fetch_interval_seconds == 900
    assert endpoint.adapter_config == {}
    with pytest.raises(IntegrityError), transaction.atomic():
        Source.objects.create(slug=source.slug, name="Duplicate")
    with pytest.raises(IntegrityError), transaction.atomic():
        SourceEndpoint.objects.create(source=source, kind=SourceEndpoint.Kind.RSS, url=endpoint.url)
    with pytest.raises(IntegrityError), transaction.atomic():
        SourceEndpoint.objects.filter(pk=endpoint.pk).update(fetch_interval_seconds=0)


@pytest.mark.django_db
def test_raw_identity_is_unique_per_endpoint_kind_key_and_payload(endpoint):
    first = make_raw(endpoint)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_raw(endpoint)
    different_revision = make_raw(endpoint, payload_hash="b" * 64, supersedes=first)
    other_namespace = make_raw(endpoint, external_key_kind=RawArticle.ExternalKeyKind.CANONICAL_URL)
    assert different_revision.supersedes_id == first.pk
    assert other_namespace.pk != first.pk
    assert RawArticle.objects.count() == 3


@pytest.mark.django_db
def test_canonical_url_is_globally_unique_across_sources(endpoint):
    raw = make_raw(endpoint)
    make_article(endpoint.source, endpoint, raw)
    other_source = Source.objects.create(slug="publisher-two", name="Publisher Two")
    other_endpoint = SourceEndpoint.objects.create(
        source=other_source,
        kind=SourceEndpoint.Kind.JSON_FEED,
        url="https://other.example/feed.json",
    )
    other_raw = make_raw(other_endpoint)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_article(other_source, other_endpoint, other_raw, external_id="other-id")


@pytest.mark.django_db
def test_provider_identity_unique_only_when_nonempty(endpoint):
    raw = make_raw(endpoint)
    make_article(endpoint.source, endpoint, raw)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_article(endpoint.source, endpoint, raw, canonical_url="https://example.com/two")
    make_article(
        endpoint.source,
        endpoint,
        raw,
        external_id="",
        canonical_url="https://example.com/three",
    )
    make_article(
        endpoint.source,
        endpoint,
        raw,
        external_id="",
        canonical_url="https://example.com/four",
    )
    assert Article.objects.count() == 3


@pytest.mark.django_db
def test_empty_canonical_url_rejected_by_database(endpoint):
    raw = make_raw(endpoint)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_article(endpoint.source, endpoint, raw, canonical_url="")


@pytest.mark.django_db
def test_article_cannot_duplicate_itself(endpoint):
    raw = make_raw(endpoint)
    article = make_article(endpoint.source, endpoint, raw)
    with pytest.raises(IntegrityError), transaction.atomic():
        Article.objects.filter(pk=article.pk).update(duplicate_of=article)


@pytest.mark.django_db
def test_provenance_deletion_is_protected(endpoint):
    raw = make_raw(endpoint)
    make_article(endpoint.source, endpoint, raw)
    with pytest.raises(ProtectedError):
        endpoint.source.delete()
    with pytest.raises(ProtectedError):
        endpoint.delete()
    with pytest.raises(ProtectedError):
        raw.delete()


@pytest.mark.django_db
def test_endpoint_validation_rejects_non_http_schemes_without_fetching(source):
    for url in ("ftp://example.com/feed", "file:///tmp/feed", "javascript:alert(1)"):
        endpoint = SourceEndpoint(source=source, kind=SourceEndpoint.Kind.RSS, url=url)
        with pytest.raises(ValidationError, match="http or https"):
            endpoint.save()
    assert SourceEndpoint.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("key", ["token", "API_KEY", "Secret", "password", "Authorization"])
def test_endpoint_validation_rejects_secret_keys_without_values(source, key):
    sentinel = "DO_NOT_LEAK_THIS_VALUE"
    endpoint = SourceEndpoint(
        source=source,
        kind=SourceEndpoint.Kind.RSS,
        url="https://example.com/feed.xml",
        adapter_config={"nested": [{key: sentinel}]},
    )
    with pytest.raises(ValidationError) as error:
        endpoint.save()
    assert key in str(error.value)
    assert sentinel not in str(error.value)


@pytest.mark.django_db
def test_endpoint_target_validation_rejects_private_dns(source, monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("10.0.0.5",))
    endpoint = SourceEndpoint(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )
    with override_settings(NEWS_FETCH_ALLOW_PRIVATE_NETWORKS=False):
        with pytest.raises(ValidationError, match="BLOCKED_TARGET"):
            endpoint.save()


@pytest.mark.django_db
def test_endpoint_target_validation_allows_public_dns(source):
    with override_settings(NEWS_FETCH_ALLOW_PRIVATE_NETWORKS=False):
        endpoint = SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
        )
    assert endpoint.pk is not None


@pytest.mark.django_db
def test_endpoint_target_validation_allows_controlled_private_dns(source, monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("127.0.0.1",))
    with override_settings(NEWS_FETCH_ALLOW_PRIVATE_NETWORKS=True):
        endpoint = SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.RSS, url="https://fixture.example/rss"
        )
    assert endpoint.pk is not None


@pytest.mark.django_db
def test_news_migrations_have_provenance_publication_and_story_tables_only():
    expected = {
        "news_source",
        "news_sourceendpoint",
        "news_ingestionrun",
        "news_rawarticle",
        "news_article",
        "news_story",
        "news_storyarticle",
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        news_tables = {row[0] for row in cursor.fetchall() if row[0].startswith("news_")}
    assert news_tables == expected
