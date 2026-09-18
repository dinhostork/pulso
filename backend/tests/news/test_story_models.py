"""Verify Story persistence and the derived Story/Article association (issue #24)."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from news.models import (
    EVIDENCE_MAX_BYTES,
    Article,
    IngestionRun,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
)


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    """Endpoint validation resolves names, so persistence tests use fake DNS."""

    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


def make_publication(slug, *, fingerprint=None, duplicate_of=None):
    """One Source with its own endpoint, ingestion run, RawArticle and Article."""

    source = Source.objects.create(slug=slug, name=slug.title())
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://{slug}.example/feed.xml"
    )
    run = IngestionRun.objects.create(endpoint=endpoint, trigger="test", started_at=timezone.now())
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        ingestion_run=run,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"{slug}-1",
        external_id=f"{slug}-1",
        url=f"https://{slug}.example/story",
        payload={"title": f"{slug} headline"},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"{slug}-1",
        canonical_url=f"https://{slug}.example/story",
        title=f"{slug} headline",
        language="en",
        content_fingerprint=fingerprint or slug[0] * 64,
        duplicate_of=duplicate_of,
        first_seen_at=timezone.now(),
    )


def associate(story, article, **overrides):
    fields = {
        "story": story,
        "article": article,
        "method": StoryArticle.Method.MATCHED,
        "similarity": 0.9,
    }
    fields.update(overrides)
    return StoryArticle.objects.create(**fields)


def table_columns(table):
    with connection.cursor() as cursor:
        return {
            column.name for column in connection.introspection.get_table_description(cursor, table)
        }


def table_constraints(table):
    with connection.cursor() as cursor:
        return connection.introspection.get_constraints(cursor, table)


def provenance_snapshot():
    return {
        model.__name__: list(model.objects.order_by("pk").values())
        for model in (Source, SourceEndpoint, IngestionRun, RawArticle, Article)
    }


@pytest.mark.django_db
def test_story_and_article_are_separate_tables_without_story_columns_on_provenance():
    assert Story._meta.db_table == "news_story"
    assert Article._meta.db_table == "news_article"
    assert table_columns("news_story") == {
        "id",
        "status",
        "language",
        "created_at",
        "updated_at",
        "refresh_state",
        "member_signature",
        "refreshed_at",
        "refresh_error",
        "article_count",
        "source_count",
        "first_published_at",
        "last_published_at",
    }
    for table in ("news_article", "news_rawarticle"):
        assert not {column for column in table_columns(table) if "story" in column}, table


@pytest.mark.django_db
def test_story_holds_articles_from_several_sources_in_deterministic_order():
    story = Story.objects.create(language="en")
    articles = [make_publication(slug) for slug in ("alpha", "bravo", "charlie")]
    start = timezone.now()
    # Inserted out of order; membership is read back by association time.
    for offset, article in ((2, articles[2]), (0, articles[0]), (1, articles[1])):
        associate(
            story,
            article,
            is_primary=True,
            associated_at=start + timedelta(seconds=offset),
            method=StoryArticle.Method.CREATED_STORY
            if offset == 0
            else StoryArticle.Method.MATCHED,
        )

    membership = story.story_articles.order_by("associated_at", "pk")
    assert [row.article_id for row in membership] == [article.pk for article in articles]
    assert len({row.article.source_id for row in membership}) == 3


@pytest.mark.django_db
def test_article_can_belong_to_a_primary_and_a_non_primary_story():
    article = make_publication("alpha")
    primary = Story.objects.create(language="en")
    secondary = Story.objects.create(language="en")
    associate(primary, article, is_primary=True)
    associate(secondary, article, is_primary=False)

    assert set(article.story_articles.values_list("story_id", "is_primary")) == {
        (primary.pk, True),
        (secondary.pk, False),
    }


@pytest.mark.django_db
def test_second_primary_association_for_an_article_fails_in_database():
    article = make_publication("alpha")
    stories = [Story.objects.create(language="en") for _ in range(4)]
    associate(stories[0], article, is_primary=True)

    with pytest.raises(IntegrityError, match="news_storyarticle_one_primary_per_article"):
        with transaction.atomic():
            associate(stories[1], article, is_primary=True)

    # Only the primary assignment is limited: further non-primary links remain legal.
    associate(stories[2], article, is_primary=False)
    associate(stories[3], article, is_primary=False)
    assert article.story_articles.filter(is_primary=True).count() == 1
    assert article.story_articles.filter(is_primary=False).count() == 2


@pytest.mark.django_db
def test_duplicate_story_article_pair_fails_in_database():
    article = make_publication("alpha")
    story = Story.objects.create(language="en")
    associate(story, article, is_primary=False)

    with pytest.raises(IntegrityError, match="news_storyarticle_story_article_unique"):
        with transaction.atomic():
            associate(story, article, is_primary=False, method=StoryArticle.Method.MANUAL)


@pytest.mark.django_db
def test_zero_member_story_is_legal_and_can_be_archived():
    story = Story.objects.create(language="en")
    assert story.status == Story.Status.ACTIVE
    assert not story.story_articles.exists()

    article = make_publication("alpha")
    membership = associate(story, article, is_primary=True)
    membership.delete()
    assert not story.story_articles.exists()

    Story.objects.filter(pk=story.pk).update(status=Story.Status.ARCHIVED)
    story.refresh_from_db()
    assert story.status == Story.Status.ARCHIVED
    assert not story.story_articles.exists()
    assert list(Story.objects.filter(status=Story.Status.ACTIVE)) == []


@pytest.mark.django_db
def test_deleting_story_removes_associations_but_not_provenance():
    articles = [make_publication(slug) for slug in ("alpha", "bravo")]
    story = Story.objects.create(language="en")
    other = Story.objects.create(language="en")
    for index, article in enumerate(articles):
        associate(story, article, is_primary=index == 0)
    kept = associate(other, articles[1], is_primary=True)
    before = provenance_snapshot()

    story.delete()

    assert not StoryArticle.objects.filter(story_id=story.pk).exists()
    assert list(StoryArticle.objects.values_list("pk", flat=True)) == [kept.pk]
    assert provenance_snapshot() == before


@pytest.mark.django_db
def test_deleting_associated_article_is_protected():
    article = make_publication("alpha")
    associate(Story.objects.create(language="en"), article, is_primary=True)

    with pytest.raises(ProtectedError):
        article.delete()
    assert Article.objects.filter(pk=article.pk).exists()


@pytest.mark.django_db
def test_oversized_evidence_fails_validation_without_echoing_it():
    article = make_publication("alpha")
    story = Story.objects.create(language="en")
    sentinel = "DO_NOT_LEAK_THIS_VALUE"
    evidence = {"note": sentinel + "x" * EVIDENCE_MAX_BYTES}

    with pytest.raises(ValidationError) as error:
        associate(story, article, evidence=evidence)
    assert "exceeds" in str(error.value)
    assert sentinel not in str(error.value)
    assert not StoryArticle.objects.exists()


@pytest.mark.django_db
def test_evidence_within_budget_is_accepted():
    article = make_publication("alpha")
    story = Story.objects.create(language="en")
    membership = associate(
        story, article, evidence={"candidates": [{"story_id": 7, "similarity": 0.81}]}
    )
    membership.refresh_from_db()
    assert membership.evidence == {"candidates": [{"story_id": 7, "similarity": 0.81}]}


@pytest.mark.django_db
@pytest.mark.parametrize("key", ["title", "body_text", "description", "payload"])
def test_publication_content_keys_fail_validation_without_echoing_values(key):
    article = make_publication("alpha")
    story = Story.objects.create(language="en")
    sentinel = "DO_NOT_LEAK_THIS_VALUE"

    for evidence in ({key: sentinel}, {"candidates": [{key: sentinel}]}):
        with pytest.raises(ValidationError) as error:
            associate(story, article, evidence=evidence)
        assert key in str(error.value)
        assert sentinel not in str(error.value)
    assert not StoryArticle.objects.exists()


@pytest.mark.django_db
def test_non_object_evidence_fails_validation():
    article = make_publication("alpha")
    story = Story.objects.create(language="en")
    with pytest.raises(ValidationError, match="object"):
        associate(story, article, evidence=["DO_NOT_LEAK_THIS_VALUE"])


@pytest.mark.django_db
def test_duplicate_of_and_fingerprints_do_not_define_story_identity():
    original = make_publication("alpha", fingerprint="f" * 64)
    republished = make_publication("bravo", fingerprint="f" * 64, duplicate_of=original)
    different = make_publication("charlie", fingerprint="0" * 64)
    first = Story.objects.create(language="en")
    second = Story.objects.create(language="en")

    # Linked by duplicate_of, yet assigned to different Stories.
    associate(first, original, is_primary=True)
    associate(second, republished, is_primary=True)
    # Different fingerprints, yet sharing one Story.
    associate(first, different, is_primary=True)

    assert republished.duplicate_of_id == original.pk
    assert original.story_articles.get().story_id != republished.story_articles.get().story_id
    assert original.content_fingerprint != different.content_fingerprint
    assert set(first.story_articles.values_list("article_id", flat=True)) == {
        original.pk,
        different.pk,
    }


@pytest.mark.django_db
def test_story_indexes_avoid_redundant_foreign_key_index():
    constraints = table_constraints("news_storyarticle")
    by_columns = {}
    for name, details in constraints.items():
        if details["index"] or details["unique"]:
            by_columns.setdefault(tuple(details["columns"]), []).append(name)

    assert ("story_id", "article_id") in by_columns
    assert ("story_id", "associated_at") in by_columns
    # Reverse lookup is served by the implicit article FK index; the partial
    # primary index cannot serve it on its own.
    assert len(by_columns[("article_id",)]) == 2
    # Every story_id lookup is covered by the composite indexes above.
    assert ("story_id",) not in by_columns
    assert "news_story_status_idx" in table_constraints("news_story")
