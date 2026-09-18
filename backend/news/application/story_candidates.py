"""Bounded, deterministic retrieval of candidate Stories for one Article (#27).

Retrieval is a recall step: it returns the Stories that are plausibly the same
event, nearest first, and never decides a match. The bounds below cap work
and are deliberately generous; the match threshold lives with the decision.

Everything happens in one PostgreSQL query. A CTE reads the Article's stored
embedding, event time and language; a lateral subquery computes the pgvector
cosine distance (`<=>`) to every Story embedding of the same `model_key`,
filters by lifecycle, language, time window and distance bound, orders by
(`distance`, `story_id`) and applies the limit. Vectors never reach Python.

The scan is exact. `StoryEmbedding.vector` is dimension-agnostic because
embedding models differ, and pgvector's approximate indexes (HNSW/IVFFlat)
need a fixed dimension; they would also make results approximate and tie
order unstable. The supporting relational indexes already exist:
`StoryEmbedding.model_key`, `Story.status` and the StoryArticle
(`story`, `associated_at`) membership index. Revisit an approximate,
per-model partial index only when Story volume makes the exact scan slow.

The function only reads.
"""

from datetime import timedelta

from django.conf import settings
from django.db import connection

from news.domain.stories import STORY_ACTIVE, StoryCandidate

# Event time is `published_at`, falling back to `first_seen_at`, for the
# Article and for every Story member alike. A Story's member time range must
# overlap [event_time - window, event_time + window].
_CANDIDATES_SQL = f"""
WITH target AS (
    SELECT
        embedding.vector AS vector,
        COALESCE(article.published_at, article.first_seen_at) AS event_time,
        lower(split_part(article.language, '-', 1)) AS language_key
    FROM news_articleembedding AS embedding
    JOIN news_article AS article ON article.id = embedding.article_id
    WHERE embedding.article_id = %(article_id)s AND embedding.model_key = %(model_key)s
)
SELECT
    candidate.story_id,
    candidate.distance,
    candidate.member_count,
    candidate.last_time,
    candidate.language,
    candidate.status
FROM target
LEFT JOIN LATERAL (
    SELECT
        story.id AS story_id,
        story_embedding.vector <=> target.vector AS distance,
        members.member_count,
        members.last_time,
        story.language,
        story.status
    FROM news_storyembedding AS story_embedding
    JOIN news_story AS story ON story.id = story_embedding.story_id
    JOIN LATERAL (
        SELECT
            count(*) AS member_count,
            min(COALESCE(member.published_at, member.first_seen_at)) AS first_time,
            max(COALESCE(member.published_at, member.first_seen_at)) AS last_time
        FROM news_storyarticle AS association
        JOIN news_article AS member ON member.id = association.article_id
        WHERE association.story_id = story.id
    ) AS members ON members.member_count > 0
    WHERE story_embedding.model_key = %(model_key)s
      AND story.status = '{STORY_ACTIVE}'
      AND lower(split_part(story.language, '-', 1)) = target.language_key
      AND members.first_time <= target.event_time + %(window)s
      AND members.last_time >= target.event_time - %(window)s
      AND story_embedding.vector <=> target.vector <= %(max_distance)s
    ORDER BY distance, story.id
    LIMIT %(limit)s
) AS candidate ON TRUE
"""


class MissingArticleEmbedding(LookupError):
    """The Article has no stored embedding for the requested `model_key`.

    Distinct from an empty result: "no similar Story exists" must never be
    confused with "this Article was never embedded".
    """

    def __init__(self, article_id: int, model_key: str):
        self.article_id = article_id
        self.model_key = model_key
        super().__init__(f"Article {article_id} has no embedding for model {model_key}.")


def find_candidates(
    article_id: int,
    model_key: str,
    *,
    limit: int | None = None,
    max_distance: float | None = None,
    window_hours: float | None = None,
) -> tuple[StoryCandidate, ...]:
    """Nearest ACTIVE, language-compatible Stories within the retrieval bounds."""

    limit = settings.NEWS_STORY_CANDIDATE_LIMIT if limit is None else limit
    if max_distance is None:
        max_distance = settings.NEWS_STORY_CANDIDATE_MAX_DISTANCE
    if window_hours is None:
        window_hours = settings.NEWS_STORY_CANDIDATE_WINDOW_HOURS
    if limit < 1:
        raise ValueError("limit must be positive")
    params = {
        "article_id": article_id,
        "model_key": model_key,
        "limit": limit,
        "max_distance": max_distance,
        "window": timedelta(hours=window_hours),
    }
    with connection.cursor() as cursor:
        cursor.execute(_CANDIDATES_SQL, params)
        rows = cursor.fetchall()
    if not rows:
        raise MissingArticleEmbedding(article_id, model_key)
    return tuple(
        StoryCandidate(
            story_id=story_id,
            distance=float(distance),
            member_count=member_count,
            last_article_published_at=last_time,
            language=language,
            status=status,
        )
        for story_id, distance, member_count, last_time, language, status in rows
        if story_id is not None
    )
