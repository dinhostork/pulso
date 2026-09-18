"""Story Topics and Entities against real PostgreSQL (#30)."""

from datetime import UTC, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from news.application.story_enrichment import extract_story_enrichment
from news.application.story_ports import (
    EnrichmentError,
    EnrichmentErrorKind,
    ExtractedEntity,
    ExtractedTopic,
    ExtractorModel,
)
from news.domain.enrichment import normalize_name
from news.models import (
    Article,
    Entity,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEntity,
    StoryTopic,
    Topic,
)

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
SECRET = "DO_NOT_LEAK_THIS_ARTICLE_TEXT"
HARBOR = (
    "Storm forces closure of Elsby harbor",
    "The Elsby Harbor Authority closed the harbor after gales damaged two piers. "
    "Harbor master Oren Vasko said engineers must inspect the storm damage.",
)
FLEET = (
    "Fishing fleet stranded as gale wrecks waterfront",
    "Trawlers sat idle in Elsby after the storm. The Elsby Harbor Authority barred traffic "
    "while engineers assess storm damage.",
)
MUSEUM = (
    "Museum lends maps to the harbor exhibition",
    "Curator Lena Havrill of the Meridian City Museum lent medieval maps for the display.",
)


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


_counter = iter(range(1, 100_000))


def make_article(title, body, *, minutes=0):
    number = next(_counter)
    source = Source.objects.create(slug=f"source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed.xml"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": title},
        payload_hash="a" * 64,
        fetched_at=NOW,
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=title,
        body_text=body,
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=NOW + timedelta(minutes=minutes),
        first_seen_at=NOW,
    )


def make_story(*articles):
    story = Story.objects.create(language="en")
    for article in articles:
        StoryArticle.objects.create(
            story=story, article=article, is_primary=True, method=StoryArticle.Method.MANUAL
        )
    return story


def topic_set(story):
    return {
        (row.topic.slug, row.score, row.model_key)
        for row in StoryTopic.objects.filter(story=story).select_related("topic")
    }


def entity_set(story):
    return {
        (row.entity.kind, row.entity.normalized_key, row.score, row.model_key)
        for row in StoryEntity.objects.filter(story=story).select_related("entity")
    }


def member_text(*articles):
    return normalize_name(" ".join(f"{a.title} {a.description} {a.body_text}" for a in articles))


class StubExtractor:
    """Deterministic double returning fixed values, or raising."""

    identity = ExtractorModel("stub", "fixed", "1")

    def __init__(self, topics=(), entities=(), error=None):
        self.topics, self.entities, self.error = tuple(topics), tuple(entities), error

    def extract_topics(self, story):
        if self.error:
            raise self.error
        return self.topics

    def extract_entities(self, story):
        if self.error:
            raise self.error
        return self.entities


@pytest.mark.django_db
def test_every_topic_and_entity_is_supported_by_member_text():
    articles = (make_article(*HARBOR), make_article(*FLEET, minutes=30))
    story = make_story(*articles)

    summary = extract_story_enrichment(story.pk)

    text = member_text(*articles)
    assert summary.topic_count == StoryTopic.objects.filter(story=story).count() > 0
    assert summary.entity_count == StoryEntity.objects.filter(story=story).count() > 0
    assert (
        summary.topic_model_key
        == summary.entity_model_key
        == "rules:capitalized-phrases-keywords@1"
    )
    for row in StoryEntity.objects.filter(story=story).select_related("entity"):
        assert row.entity.normalized_key in text
        assert normalize_name(row.entity.display_name) == row.entity.normalized_key
    for row in StoryTopic.objects.filter(story=story).select_related("topic"):
        assert row.topic.label in text.split()
    assert ("ORGANIZATION", "elsby harbor authority", 1.0, summary.entity_model_key) in (
        entity_set(story)
    )


@pytest.mark.django_db
def test_removing_a_member_removes_what_only_it_supported():
    harbor, museum = make_article(*HARBOR), make_article(*MUSEUM, minutes=5)
    story = make_story(harbor, museum)
    extract_story_enrichment(story.pk)
    keys = {key for _kind, key, _score, _model in entity_set(story)}
    assert {"lena havrill", "meridian city museum"} <= keys

    StoryArticle.objects.filter(article=museum).delete()
    extract_story_enrichment(story.pk)

    keys = {key for _kind, key, _score, _model in entity_set(story)}
    assert not {"lena havrill", "meridian city museum"} & keys
    assert "elsby harbor authority" in keys
    remaining = member_text(harbor)
    assert all(slug in remaining.split() for slug, _score, _model in topic_set(story))
    # Reference vocabulary stays; only the Story's derived links went.
    assert Entity.objects.filter(normalized_key="lena havrill").exists()


@pytest.mark.django_db
def test_rerunning_on_unchanged_membership_is_logically_identical():
    story = make_story(make_article(*HARBOR), make_article(*FLEET, minutes=30))
    extract_story_enrichment(story.pk)
    topics, entities = topic_set(story), entity_set(story)
    vocabulary = (Topic.objects.count(), Entity.objects.count())

    for _ in range(2):
        extract_story_enrichment(story.pk)

    assert (topic_set(story), entity_set(story)) == (topics, entities)
    assert (Topic.objects.count(), Entity.objects.count()) == vocabulary
    assert StoryTopic.objects.filter(story=story).count() == len(topics)
    assert StoryEntity.objects.filter(story=story).count() == len(entities)


@pytest.mark.django_db
def test_deleting_derived_rows_and_rebuilding_reproduces_them_and_keeps_provenance():
    story = make_story(make_article(*HARBOR), make_article(*FLEET, minutes=30))
    extract_story_enrichment(story.pk)
    topics, entities = topic_set(story), entity_set(story)
    before = (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    )

    StoryTopic.objects.all().delete()
    StoryEntity.objects.all().delete()
    extract_story_enrichment(story.pk)

    assert (topic_set(story), entity_set(story)) == (topics, entities)
    assert (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    ) == before


@pytest.mark.django_db
def test_case_and_whitespace_variants_across_articles_become_one_entity():
    first = make_article("Rates rise", "The Central Bank raised rates. CENTRAL BANK staff agreed.")
    second = make_article("Markets react", "Traders expect the Central  Bank to pause.", minutes=1)
    story = make_story(first, second)

    extract_story_enrichment(story.pk)

    banks = Entity.objects.filter(normalized_key="central bank")
    assert banks.count() == 1
    bank = banks.get()
    assert bank.kind == Entity.Kind.ORGANIZATION
    assert StoryEntity.objects.get(story=story, entity=bank).score == 1.0


@pytest.mark.django_db
def test_database_rejects_duplicate_vocabulary_and_links():
    story = make_story(make_article(*HARBOR))
    topic = Topic.objects.create(slug="storm", label="storm")
    entity = Entity.objects.create(kind="PLACE", normalized_key="elsby", display_name="Elsby")
    StoryTopic.objects.create(story=story, topic=topic, score=1.0, model_key="stub:fixed@1")
    StoryEntity.objects.create(story=story, entity=entity, score=1.0, model_key="stub:fixed@1")

    for create in (
        lambda: Entity.objects.create(kind="PLACE", normalized_key="elsby", display_name="ELSBY"),
        lambda: Topic.objects.create(slug="storm", label="Storm"),
        lambda: StoryTopic.objects.create(
            story=story, topic=topic, score=0.5, model_key="stub:other@1"
        ),
        lambda: StoryEntity.objects.create(
            story=story, entity=entity, score=0.5, model_key="stub:other@1"
        ),
        lambda: StoryTopic.objects.create(story=make_story(), topic=topic, score=1.0, model_key=""),
        lambda: StoryEntity.objects.create(
            story=make_story(), entity=entity, score=1.5, model_key="stub:fixed@1"
        ),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            create()
    # The same name under another kind is a different Entity.
    Entity.objects.create(kind="OTHER", normalized_key="elsby", display_name="Elsby")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("extractor", "kind"),
    [
        (StubExtractor(error=RuntimeError(f"model crashed on {SECRET}")), "EXTRACTOR_FAILED"),
        (StubExtractor(topics=[ExtractedTopic("storm", 2.0)]), "INVALID_OUTPUT"),
        (StubExtractor(entities=[ExtractedEntity("CLAIM", "Elsby", 1.0)]), "INVALID_OUTPUT"),
        (StubExtractor(topics=[ExtractedTopic("...", 1.0)]), "INVALID_OUTPUT"),
    ],
)
def test_extractor_failure_keeps_the_previous_rows_and_is_diagnosable(
    extractor, kind, pulso_caplog
):
    story = make_story(make_article(HARBOR[0], HARBOR[1] + " " + SECRET))
    extract_story_enrichment(story.pk)
    before = (
        list(StoryTopic.objects.order_by("pk").values()),
        list(StoryEntity.objects.order_by("pk").values()),
    )

    with pytest.raises(EnrichmentError) as error:
        extract_story_enrichment(story.pk, topic_extractor=extractor, entity_extractor=extractor)

    assert error.value.kind == kind
    assert error.value.model_key == "stub:fixed@1"
    assert SECRET not in str(error.value) and len(str(error.value)) <= 200
    assert (
        list(StoryTopic.objects.order_by("pk").values()),
        list(StoryEntity.objects.order_by("pk").values()),
    ) == before
    (record,) = [r for r in pulso_caplog.records if r.message == "News Story enrichment failed"]
    assert (record.story_id, record.error_kind) == (story.pk, kind)
    assert SECRET not in pulso_caplog.text


@pytest.mark.django_db
def test_story_without_members_is_an_explicit_error():
    story = make_story()
    with pytest.raises(EnrichmentError) as error:
        extract_story_enrichment(story.pk)
    assert error.value.kind == EnrichmentErrorKind.NO_MEMBERS
    assert not StoryTopic.objects.exists() and not StoryEntity.objects.exists()


@pytest.mark.django_db
def test_input_is_bounded_and_read_in_publication_order():
    later = make_article(*MUSEUM, minutes=60)
    earlier = make_article(*HARBOR)
    story = make_story(later, earlier)

    with override_settings(NEWS_STORY_ENRICHMENT_MAX_ARTICLES=1):
        extract_story_enrichment(story.pk)
    keys = {key for _kind, key, _score, _model in entity_set(story)}
    assert "elsby harbor authority" in keys and "lena havrill" not in keys

    with override_settings(NEWS_STORY_ENRICHMENT_MAX_CHARS_PER_ARTICLE=len(HARBOR[0])):
        extract_story_enrichment(story.pk)
    keys = {key for _kind, key, _score, _model in entity_set(story)}
    assert "oren vasko" not in keys and "lena havrill" not in keys


@pytest.mark.django_db
def test_stub_extractor_output_is_normalized_and_stamped():
    story = make_story(make_article(*HARBOR))
    stub = StubExtractor(
        topics=[ExtractedTopic("Storm  Damage", 0.5)],
        entities=[
            ExtractedEntity("PERSON", "Oren Vasko", 1.0),
            ExtractedEntity("PERSON", "oren  VASKO", 0.25),
        ],
    )

    extract_story_enrichment(story.pk, topic_extractor=stub, entity_extractor=stub)

    assert topic_set(story) == {("storm-damage", 0.5, "stub:fixed@1")}
    assert entity_set(story) == {("PERSON", "oren vasko", 1.0, "stub:fixed@1")}
    assert Entity.objects.get(normalized_key="oren vasko").display_name == "Oren Vasko"
    for row in (*StoryTopic.objects.all(), *StoryEntity.objects.all()):
        assert row.model_key and row.generated_at is not None
        assert abs(row.generated_at - timezone.now()) < timedelta(minutes=1)


def test_topic_and_entity_are_vocabulary_not_facts():
    assert [field.name for field in Topic._meta.concrete_fields] == [
        "id",
        "slug",
        "label",
        "created_at",
    ]
    assert [field.name for field in Entity._meta.concrete_fields] == [
        "id",
        "kind",
        "normalized_key",
        "display_name",
        "created_at",
    ]
    for model in (Topic, Entity):
        assert not [field for field in model._meta.concrete_fields if field.is_relation]
        assert Source not in {field.related_model for field in model._meta.get_fields()}
