"""Story matching persistence against real PostgreSQL/pgvector (#28).

Vectors are placed by hand on the unit circle: a Story at angle θ from the
Article is at cosine distance 1 - cos θ, so 10° (0.015) is well within the
0.18 threshold, 40° (0.234) is in the ambiguous band and 70° (0.658) is beyond
the 0.5 retrieval bound.
"""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from news.adapters.deterministic_embeddings import DeterministicEmbeddingProvider
from news.application import story_matching as matching_module
from news.application.embeddings import embed_article
from news.application.story_candidates import MissingArticleEmbedding, find_candidates
from news.application.story_matching import (
    EVIDENCE_CANDIDATES,
    MATCHER_KEY,
    MatchState,
    match_article,
)
from news.domain.stories import StoryCandidate
from news.domain.story_matching import MatchKind, MatchReason
from news.models import (
    EVIDENCE_MAX_BYTES,
    Article,
    ArticleEmbedding,
    IngestionRun,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
)

KEY = "manual:matching-check@1"
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
SECRET = "DO_NOT_COPY_THIS_TEXT"


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    """Endpoint validation resolves names, so persistence tests use fake DNS."""

    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


def at(degrees):
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)]


_counter = iter(range(1, 100_000))


def make_article(degrees=None, *, published_at=NOW, language="en", duplicate_of=None, text=""):
    number = next(_counter)
    source = Source.objects.create(slug=f"source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed.xml"
    )
    run = IngestionRun.objects.create(endpoint=endpoint, trigger="test", started_at=NOW)
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        ingestion_run=run,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": text or "t"},
        payload_hash="a" * 64,
        fetched_at=NOW,
    )
    article = Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=text or f"Article {number}",
        body_text=text,
        language=language,
        content_fingerprint="f" * 64 if duplicate_of else f"{number:064d}",
        duplicate_of=duplicate_of,
        published_at=published_at,
        first_seen_at=published_at,
    )
    if degrees is not None:
        ArticleEmbedding.objects.create(
            article=article, model_key=KEY, dimension=2, vector=at(degrees), input_chars=1
        )
    return article


def match(article):
    return match_article(article.pk, model_key=KEY)


def provenance():
    return [
        list(model.objects.order_by("pk").values())
        for model in (Article, RawArticle, IngestionRun, Source, SourceEndpoint)
    ]


@pytest.mark.django_db
def test_no_candidate_creates_one_active_story_with_its_first_association():
    article = make_article(0)

    outcome = match(article)

    story = Story.objects.get()
    association = StoryArticle.objects.get()
    assert outcome.state is MatchState.CREATED_STORY
    assert outcome.decision.kind is MatchKind.CREATE_NEW_STORY
    assert (outcome.story_id, outcome.association_id) == (story.pk, association.pk)
    assert story.status == Story.Status.ACTIVE and story.language == "en"
    assert association.is_primary and association.article_id == article.pk
    assert association.method == StoryArticle.Method.CREATED_STORY
    assert association.similarity is None
    assert association.matcher_key == MATCHER_KEY
    assert association.evidence["reason"] == MatchReason.NO_CANDIDATES
    embedding = StoryEmbedding.objects.get(story=story)
    assert (embedding.model_key, embedding.dimension, embedding.member_count) == (KEY, 2, 1)
    assert embedding.vector == pytest.approx(at(0), abs=1e-6)


@pytest.mark.django_db
def test_nearby_article_joins_the_story_with_persisted_evidence():
    first = make_article(0)
    created = match(first)
    second = make_article(10, published_at=NOW + timedelta(hours=3))

    outcome = match(second)

    distance = 1 - math.cos(math.radians(10))
    association = StoryArticle.objects.get(article=second)
    assert outcome.state is MatchState.MATCHED
    assert outcome.story_id == created.story_id == association.story_id
    assert Story.objects.count() == 1
    assert association.is_primary and association.method == StoryArticle.Method.MATCHED
    assert association.similarity == pytest.approx(1 - distance, abs=1e-6)
    assert association.matcher_key == MATCHER_KEY
    evidence = association.evidence
    assert evidence["reason"] == MatchReason.WITHIN_THRESHOLD
    assert evidence["distance"] == pytest.approx(distance, abs=1e-6)
    assert evidence["candidate_count"] == 1
    assert evidence["candidates"] == [
        {"story_id": created.story_id, "distance": evidence["distance"]}
    ]
    assert (evidence["max_distance"], evidence["max_time_gap_hours"]) == (0.18, 48)
    assert evidence["embedding_model_key"] == KEY
    # The existing Story's embedding is not refreshed here (#32 owns refresh).
    assert StoryEmbedding.objects.get().member_count == 1


@pytest.mark.django_db
def test_ambiguous_band_starts_a_new_story_and_records_why():
    match(make_article(0))
    ambiguous = make_article(40)

    outcome = match(ambiguous)

    assert outcome.state is MatchState.CREATED_STORY
    assert outcome.decision.reason is MatchReason.ABOVE_THRESHOLD
    association = StoryArticle.objects.get(article=ambiguous)
    assert association.evidence["reason"] == MatchReason.ABOVE_THRESHOLD
    assert association.evidence["distance"] == pytest.approx(
        1 - math.cos(math.radians(40)), abs=1e-6
    )
    assert Story.objects.count() == 2


@pytest.mark.django_db
def test_replay_returns_the_existing_association_without_new_rows():
    first = match(make_article(0))
    article = make_article(5)
    matched = match(article)
    counts = (Story.objects.count(), StoryArticle.objects.count(), StoryEmbedding.objects.count())

    for _ in range(3):
        replay = match(article)
        assert replay.state is MatchState.ALREADY_ASSIGNED
        assert (replay.story_id, replay.association_id) == (first.story_id, matched.association_id)
    assert (
        Story.objects.count(),
        StoryArticle.objects.count(),
        StoryEmbedding.objects.count(),
    ) == counts


@pytest.mark.django_db
def test_missing_embedding_raises_and_writes_nothing():
    bare = make_article()
    match(make_article(0))
    before = (Story.objects.count(), StoryArticle.objects.count())

    with pytest.raises(MissingArticleEmbedding):
        match(bare)
    assert (Story.objects.count(), StoryArticle.objects.count()) == before


@pytest.mark.django_db
def test_matching_never_writes_publication_provenance():
    articles = [make_article(degrees, text=SECRET) for degrees in (0, 5, 60, 8)]
    before = provenance()

    for article in articles:
        match(article)

    assert provenance() == before


@pytest.mark.django_db
def test_evidence_is_bounded_identifiers_and_numbers_only():
    for degrees in range(0, 60, 2):
        story = Story.objects.create(language="en")
        member = make_article()
        StoryArticle.objects.create(
            story=story, article=member, is_primary=True, method=StoryArticle.Method.MANUAL
        )
        StoryEmbedding.objects.create(
            story=story, model_key=KEY, dimension=2, vector=at(degrees), member_count=1
        )
    article = make_article(1, text=SECRET)

    match(article)

    evidence = StoryArticle.objects.get(article=article).evidence
    encoded = json.dumps(evidence)
    assert SECRET not in encoded and "Article" not in encoded
    assert len(evidence["candidates"]) == EVIDENCE_CANDIDATES
    assert evidence["candidate_count"] == 10  # NEWS_STORY_CANDIDATE_LIMIT
    assert len(encoded.encode()) < EVIDENCE_MAX_BYTES
    assert set(evidence) == {
        "reason",
        "distance",
        "candidate_count",
        "candidates",
        "max_distance",
        "max_time_gap_hours",
        "embedding_model_key",
    }


@pytest.mark.django_db
def test_archived_story_is_never_joined_through_retrieval():
    archived = Story.objects.get(pk=match(make_article(0)).story_id)
    Story.objects.filter(pk=archived.pk).update(status=Story.Status.ARCHIVED)
    article = make_article(0)

    outcome = match(article)

    assert outcome.state is MatchState.CREATED_STORY
    assert outcome.story_id != archived.pk
    assert StoryArticle.objects.filter(story=archived).count() == 1


@pytest.mark.django_db
def test_story_archived_after_retrieval_is_rejected_under_its_lock(monkeypatch):
    target = match(make_article(0)).story_id
    article = make_article(3)
    stale = find_candidates(article.pk, KEY)
    Story.objects.filter(pk=target).update(status=Story.Status.ARCHIVED)
    calls = []

    def retrieval(article_id, model_key):
        # The first call sees the Story as it was before it was archived.
        calls.append(article_id)
        return stale if len(calls) == 1 else find_candidates(article_id, model_key)

    monkeypatch.setattr(matching_module, "find_candidates", retrieval)

    outcome = match(article)

    assert len(calls) == 2
    assert stale[0] == StoryCandidate(
        story_id=target,
        distance=stale[0].distance,
        member_count=1,
        last_article_published_at=NOW,
        language="en",
    )
    assert outcome.state is MatchState.CREATED_STORY
    assert outcome.story_id != target
    assert StoryArticle.objects.filter(story_id=target).count() == 1
    assert Story.objects.get(pk=target).status == Story.Status.ARCHIVED


@pytest.mark.django_db
def test_duplicate_of_does_not_decide_the_story():
    original = make_article(0)
    copy = make_article(90, duplicate_of=original)
    unrelated_twin = make_article(1)

    stories = [match(article).story_id for article in (original, copy, unrelated_twin)]

    assert copy.duplicate_of_id == original.pk
    assert stories[0] != stories[1]  # linked by duplicate_of, far apart semantically
    assert stories[0] == stories[2]  # no publication link, same event evidence


@pytest.mark.django_db
def test_non_primary_associations_stay_representable():
    first = match(make_article(0))
    other = Story.objects.create(language="en")
    article = make_article(2)
    StoryArticle.objects.create(
        story=other, article=article, is_primary=False, method=StoryArticle.Method.MANUAL
    )

    outcome = match(article)

    assert outcome.story_id == first.story_id
    assert set(article.story_articles.values_list("story_id", "is_primary")) == {
        (first.story_id, True),
        (other.pk, False),
    }


@pytest.mark.django_db
def test_default_model_key_comes_from_the_configured_provider():
    article = make_article(text="Harbor closes after storm damage")
    embed_article(article.pk, DeterministicEmbeddingProvider())

    outcome = match_article(article.pk)

    assert outcome.state is MatchState.CREATED_STORY
    assert (
        StoryEmbedding.objects.get().model_key == DeterministicEmbeddingProvider.identity.model_key
    )
