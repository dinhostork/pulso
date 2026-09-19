"""Story candidate retrieval against real PostgreSQL/pgvector (issue #27).

Vectors are placed by hand on the unit circle, so the cosine distance of a
Story at angle θ from the Article is exactly 1 - cos θ.
"""

import dataclasses
import math
import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.db import close_old_connections, connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from pgvector import Vector

from news.application.story_candidates import MissingArticleEmbedding, find_candidates
from news.domain.stories import STORY_ACTIVE, STORY_ARCHIVED, StoryCandidate, language_key
from news.models import (
    Article,
    ArticleEmbedding,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
)

KEY = "manual:retrieval-check@1"
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    """Endpoint validation resolves names, so persistence tests use fake DNS."""

    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


def at(degrees):
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)]


def distance_at(degrees):
    return 1 - math.cos(math.radians(degrees))


_counter = iter(range(1, 100_000))


def make_article(*, published_at=NOW, first_seen_at=NOW, language="en"):
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
        payload={"title": "t"},
        payload_hash="a" * 64,
        fetched_at=NOW,
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=f"Article {number}",
        language=language,
        content_fingerprint=f"{number:064d}",
        published_at=published_at,
        first_seen_at=first_seen_at,
    )


def incoming(degrees=0, **fields):
    article = make_article(**fields)
    ArticleEmbedding.objects.create(
        article=article, model_key=KEY, dimension=2, vector=at(degrees), input_chars=1
    )
    return article


def make_story(
    degrees,
    *,
    member_times=(NOW,),
    status=STORY_ACTIVE,
    language="en",
    model_key=KEY,
):
    story = Story.objects.create(status=status, language=language)
    for index, published_at in enumerate(member_times):
        StoryArticle.objects.create(
            story=story,
            article=make_article(published_at=published_at, language=language),
            is_primary=True,
            method=StoryArticle.Method.CREATED_STORY if index == 0 else StoryArticle.Method.MATCHED,
        )
    StoryEmbedding.objects.create(
        story=story,
        model_key=model_key,
        dimension=2,
        vector=at(degrees),
        member_count=max(len(member_times), 1),
    )
    return story


def ids(candidates):
    return [candidate.story_id for candidate in candidates]


@pytest.mark.django_db
def test_nearest_stories_come_first_and_a_far_story_is_excluded():
    article = incoming()
    far = make_story(120)  # distance 1.5, beyond the 0.5 retrieval bound
    third = make_story(55)
    first = make_story(10)
    second = make_story(30)

    candidates = find_candidates(article.pk, KEY)

    assert ids(candidates) == [first.pk, second.pk, third.pk]
    assert far.pk not in ids(candidates)
    for candidate, degrees in zip(candidates, (10, 30, 55), strict=True):
        assert candidate.distance == pytest.approx(distance_at(degrees), abs=1e-6)
    assert [c.distance for c in candidates] == sorted(c.distance for c in candidates)


@pytest.mark.django_db
def test_candidate_carries_decision_evidence_and_no_model_instance():
    article = incoming()
    story = make_story(20, member_times=(NOW - timedelta(hours=5), NOW - timedelta(hours=1)))

    (candidate,) = find_candidates(article.pk, KEY)

    assert isinstance(candidate, StoryCandidate)
    assert candidate == StoryCandidate(
        story_id=story.pk,
        distance=candidate.distance,
        member_count=2,
        last_article_published_at=NOW - timedelta(hours=1),
        language="en",
        status=STORY_ACTIVE,
        first_article_published_at=NOW - timedelta(hours=5),
    )
    assert candidate.distance == pytest.approx(distance_at(20), abs=1e-6)
    assert all(
        isinstance(getattr(candidate, field.name), (int, float, str, datetime))
        for field in dataclasses.fields(candidate)
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.distance = 0.0


@pytest.mark.django_db
def test_limit_returns_exactly_the_closest_stories():
    article = incoming()
    stories = {degrees: make_story(degrees) for degrees in range(2, 50, 4)}

    with override_settings(NEWS_STORY_CANDIDATE_LIMIT=4):
        candidates = find_candidates(article.pk, KEY)

    assert len(stories) > 4
    assert ids(candidates) == [stories[degrees].pk for degrees in (2, 6, 10, 14)]
    assert ids(find_candidates(article.pk, KEY, limit=2)) == [stories[2].pk, stories[6].pk]


@pytest.mark.django_db
def test_equal_distances_break_ties_by_story_id():
    article = incoming()
    later_created = [make_story(25) for _ in range(3)]
    mirrored = make_story(-25)  # same distance on the other side of the circle

    candidates = find_candidates(article.pk, KEY)

    expected = sorted(story.pk for story in (*later_created, mirrored))
    assert ids(candidates) == expected
    assert len({candidate.distance for candidate in candidates}) == 1


@pytest.mark.django_db(transaction=True)
def test_a_fresh_connection_returns_the_identical_sequence():
    article = incoming()
    for degrees in (25, 25, -25, 10, 40, 10):
        make_story(degrees)
    here = find_candidates(article.pk, KEY)
    results = {}

    def worker():
        close_old_connections()
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                results["pid"] = cursor.fetchone()[0]
            results["candidates"] = find_candidates(article.pk, KEY)
        finally:
            connections.close_all()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=60)

    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        assert results["pid"] != cursor.fetchone()[0]
    assert results["candidates"] == here
    assert len(here) == 6


@pytest.mark.django_db
def test_other_embedding_models_are_never_compared():
    article = incoming()
    other_model = make_story(0, model_key="manual:other-model@1")
    same_model = make_story(40)

    assert ids(find_candidates(article.pk, KEY)) == [same_model.pk]
    assert other_model.pk not in ids(find_candidates(article.pk, KEY, max_distance=2.0))


@pytest.mark.django_db
def test_time_window_is_relative_to_the_story_members_not_the_clock():
    article = incoming(published_at=NOW)
    stale = make_story(0, member_times=(NOW - timedelta(days=10),))
    future = make_story(0, member_times=(NOW + timedelta(days=10),))
    spanning = make_story(15, member_times=(NOW - timedelta(days=9), NOW - timedelta(days=6)))
    edge = make_story(30, member_times=(NOW - timedelta(hours=168),))

    candidates = find_candidates(article.pk, KEY)

    assert ids(candidates) == [spanning.pk, edge.pk]
    assert {stale.pk, future.pk}.isdisjoint(ids(candidates))
    with override_settings(NEWS_STORY_CANDIDATE_WINDOW_HOURS=24 * 30):
        assert set(ids(find_candidates(article.pk, KEY))) == {
            stale.pk,
            future.pk,
            spanning.pk,
            edge.pk,
        }


@pytest.mark.django_db
def test_first_seen_at_stands_in_for_a_missing_publication_time():
    article = incoming(published_at=None, first_seen_at=NOW - timedelta(days=20))
    near_first_seen = make_story(10, member_times=(NOW - timedelta(days=20),))
    near_now = make_story(0, member_times=(NOW,))

    assert ids(find_candidates(article.pk, KEY)) == [near_first_seen.pk]
    assert near_now.pk not in ids(find_candidates(article.pk, KEY))


@pytest.mark.django_db
def test_archived_and_memberless_stories_are_never_candidates():
    article = incoming()
    archived = make_story(0, status=STORY_ARCHIVED)
    archived_empty = make_story(0, member_times=(), status=STORY_ARCHIVED)
    active_empty = make_story(0, member_times=())
    eligible = make_story(35)

    candidates = find_candidates(article.pk, KEY, max_distance=2.0)

    assert ids(candidates) == [eligible.pk]
    assert not StoryArticle.objects.filter(story=archived_empty).exists()
    assert {archived.pk, archived_empty.pk, active_empty.pk}.isdisjoint(ids(candidates))


@pytest.mark.django_db
def test_only_language_compatible_stories_are_candidates():
    article = incoming(language="en")
    regional = make_story(10, language="en-GB")
    other = make_story(0, language="pt")

    assert ids(find_candidates(article.pk, KEY)) == [regional.pk]
    assert other.pk not in ids(find_candidates(article.pk, KEY))
    assert language_key(" EN-us ") == "en"


@pytest.mark.django_db
def test_nothing_in_bounds_returns_an_empty_tuple():
    article = incoming()
    make_story(170)
    assert find_candidates(article.pk, KEY) == ()


@pytest.mark.django_db
def test_missing_article_embedding_is_an_explicit_error_not_an_empty_result():
    embedded = incoming()
    bare = make_article()
    make_story(10)

    for article_id, model_key in ((bare.pk, KEY), (embedded.pk, "manual:never-used@1")):
        with pytest.raises(MissingArticleEmbedding) as error:
            find_candidates(article_id, model_key)
        assert (error.value.article_id, error.value.model_key) == (article_id, model_key)
        assert str(article_id) in str(error.value) and model_key in str(error.value)


@pytest.mark.django_db
def test_retrieval_is_one_query_with_distance_order_and_limit_in_sql():
    article = incoming()
    for degrees in (5, 15, 25):
        make_story(degrees)

    with CaptureQueriesContext(connections["default"]) as queries:
        candidates = find_candidates(article.pk, KEY)

    assert len(candidates) == 3
    assert len(queries.captured_queries) == 1
    sql = queries.captured_queries[0]["sql"]
    assert "<=>" in sql
    assert "ORDER BY distance, story.id" in sql
    assert "LIMIT 10" in sql


@pytest.mark.django_db
def test_vectors_deserialized_in_python_are_bounded_by_the_limit(monkeypatch):
    article = incoming()
    for index in range(60):
        make_story(index % 50)
    decoded = []
    from_text = Vector._from_text.__func__

    def counting(cls, value):
        decoded.append(value)
        return from_text(cls, value)

    monkeypatch.setattr(Vector, "_from_text", classmethod(counting))

    candidates = find_candidates(article.pk, KEY, limit=3)

    assert len(candidates) == 3
    assert StoryEmbedding.objects.count() == 60
    # No vector crosses into Python at all, which is within any limit.
    assert len(decoded) == 0 <= 3
    # Control: loading one Story vector through the ORM is counted.
    assert len(StoryEmbedding.objects.order_by("pk").first().vector) == 2
    assert len(decoded) == 1


@pytest.mark.django_db
def test_retrieval_writes_nothing():
    article = incoming()
    make_story(10)
    make_story(0, member_times=(), status=STORY_ARCHIVED)
    models = (Story, StoryArticle, StoryEmbedding, ArticleEmbedding, Article, RawArticle)
    before = [list(model.objects.order_by("pk").values()) for model in models]

    with CaptureQueriesContext(connections["default"]) as queries:
        find_candidates(article.pk, KEY)
        find_candidates(article.pk, KEY, max_distance=2.0)

    after = [list(model.objects.order_by("pk").values()) for model in models]
    assert after == before
    for query in queries.captured_queries:
        assert query["sql"].lstrip().upper().startswith(("WITH", "SELECT"))
