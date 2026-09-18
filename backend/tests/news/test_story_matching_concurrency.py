"""Story matching races on genuinely separate PostgreSQL connections (#28).

Workers are held at a barrier after candidate retrieval, so every worker has
decided before any of them writes. PostgreSQL uniqueness arbitrates; no lock
outside the database is involved.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from django.db import close_old_connections, connections

from news.application import story_matching as matching_module
from news.application.story_matching import MatchState, match_article
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

KEY = "manual:matching-race@1"
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


def make_article(number):
    source = Source.objects.create(slug=f"race-{number}", name=f"Race {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://r{number}.example/feed.xml"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"race-{number}",
        external_id=f"race-{number}",
        url=f"https://r{number}.example/item",
        payload={"title": "t"},
        payload_hash="a" * 64,
        fetched_at=NOW,
    )
    article = Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"race-{number}",
        canonical_url=f"https://r{number}.example/item",
        title="Same event",
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=NOW,
        first_seen_at=NOW,
    )
    # Every Article sits on the same point: they are unmistakably one event.
    ArticleEmbedding.objects.create(
        article=article, model_key=KEY, dimension=2, vector=[1.0, 0.0], input_chars=1
    )
    return article


def race(monkeypatch, article_ids):
    """Match concurrently; the first retrieval of each worker waits at a barrier."""

    barrier = threading.Barrier(len(article_ids), timeout=30)
    retrieve = matching_module.find_candidates
    waiting = threading.local()

    def barriered(article_id, model_key):
        candidates = retrieve(article_id, model_key)
        if not getattr(waiting, "done", False):
            waiting.done = True
            barrier.wait()
        return candidates

    def worker(article_id):
        close_old_connections()
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            return match_article(article_id, model_key=KEY), pid
        finally:
            connections.close_all()

    with monkeypatch.context() as patch:
        patch.setattr(matching_module, "find_candidates", barriered)
        with ThreadPoolExecutor(max_workers=len(article_ids)) as pool:
            futures = [pool.submit(worker, article_id) for article_id in article_ids]
            return [future.result(timeout=60) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_same_article_matched_concurrently_gets_exactly_one_association(monkeypatch):
    article = make_article(1)

    results = race(monkeypatch, [article.pk, article.pk])

    outcomes = [outcome for outcome, _ in results]
    assert len({pid for _, pid in results}) == 2
    assert sorted(outcome.state for outcome in outcomes) == sorted(
        [MatchState.CREATED_STORY, MatchState.ALREADY_ASSIGNED]
    )
    association = StoryArticle.objects.get()
    assert {(o.story_id, o.association_id) for o in outcomes} == {
        (association.story_id, association.pk)
    }
    # The loser's Story and StoryEmbedding were rolled back with its savepoint.
    assert Story.objects.count() == 1
    assert StoryEmbedding.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_same_event_articles_matched_concurrently_create_two_stories(monkeypatch):
    """Documented v0.3 outcome: both find no Story, so both create one.

    The duplicate is accepted rather than prevented with a lock; the two Stories
    converge only through later reprocessing. Until then, later Articles join
    deterministically by (distance, story_id).
    """

    first, second = make_article(1), make_article(2)

    results = race(monkeypatch, [first.pk, second.pk])

    outcomes = [outcome for outcome, _ in results]
    assert len({pid for _, pid in results}) == 2
    assert [outcome.state for outcome in outcomes] == [MatchState.CREATED_STORY] * 2
    stories = list(Story.objects.order_by("pk"))
    assert len(stories) == 2
    assert all(story.status == Story.Status.ACTIVE for story in stories)
    for story in stories:
        (association,) = StoryArticle.objects.filter(story=story)
        assert association.is_primary
        assert association.method == StoryArticle.Method.CREATED_STORY
    assert {outcome.story_id for outcome in outcomes} == {story.pk for story in stories}
    assert StoryEmbedding.objects.count() == 2

    later = make_article(3)
    joined = match_article(later.pk, model_key=KEY)
    assert joined.state is MatchState.MATCHED
    assert joined.story_id == stories[0].pk  # equal distance: lower story_id
