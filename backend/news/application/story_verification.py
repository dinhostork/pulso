"""Gather bounded evidence for the secondary Story verifier (#36).

`news.domain.story_matching.secondary_candidates` names the candidates the
secondary rule would verify (at most NEWS_STORY_CANDIDATE_LIMIT, each already
time- and language-compatible). For those only, this module reads:

- the incoming Article's title and bounded text;
- each candidate's NEWS_STORY_MATCH_VERIFY_MAX_MEMBERS most recent members
  (by `published_at`, else `first_seen_at`, then id), capped per Story in SQL
  with a window function, with their titles and bounded text;
- the cosine distance between the Article's stored embedding and each of those
  members' embeddings for the same `model_key`, computed in PostgreSQL.

That is three queries whatever the number of candidates, and vectors never
reach Python. Text is bounded with the #25 embedding input rule
(`article_embedding_input`, NEWS_EMBEDDING_MAX_INPUT_CHARS) and turned into
proper-name anchors (`news.domain.event_anchors`); only counts and distances
leave this module. Topic, Entity, `duplicate_of` and Source are never read.
"""

from collections import defaultdict
from collections.abc import Sequence

from django.conf import settings
from django.db.models import F, Subquery, Window
from django.db.models.functions import Coalesce, RowNumber
from pgvector.django import CosineDistance

from news.domain.embeddings import article_embedding_input
from news.domain.event_anchors import EventAnchors, event_anchors, shared_anchor_count
from news.domain.stories import StoryCandidate
from news.domain.story_matching import CandidateEvidence, MatchPolicy
from news.models import Article, ArticleEmbedding, StoryArticle


def _anchors(title: str, description: str, body_text: str) -> EventAnchors:
    limit = settings.NEWS_EMBEDDING_MAX_INPUT_CHARS
    return event_anchors(
        title=" ".join(title.split())[:limit],
        text=article_embedding_input(
            title="", description=description, body_text=body_text, max_chars=limit
        ),
    )


def gather_evidence(
    article_id: int,
    model_key: str,
    candidates: Sequence[StoryCandidate],
    policy: MatchPolicy,
) -> dict[int, CandidateEvidence]:
    """Evidence per candidate Story id; empty when there is nothing to verify."""

    if not candidates:
        return {}
    story_ids = [candidate.story_id for candidate in candidates]
    article = _anchors(
        *Article.objects.values_list("title", "description", "body_text").get(pk=article_id)
    )
    event_time = Coalesce("article__published_at", "article__first_seen_at")
    members = list(
        StoryArticle.objects.filter(story_id__in=story_ids)
        .exclude(article_id=article_id)
        .annotate(
            recency=Window(
                RowNumber(),
                partition_by=[F("story_id")],
                order_by=[event_time.desc(), F("article_id").desc()],
            )
        )
        .filter(recency__lte=policy.max_members)
        .order_by("story_id", "recency")
        .values_list(
            "story_id",
            "article_id",
            "article__title",
            "article__description",
            "article__body_text",
        )
    )
    target = ArticleEmbedding.objects.filter(article_id=article_id, model_key=model_key).values(
        "vector"
    )[:1]
    distances = dict(
        ArticleEmbedding.objects.filter(
            article_id__in={member[1] for member in members}, model_key=model_key
        )
        .annotate(distance=CosineDistance("vector", Subquery(target)))
        .values_list("article_id", "distance")
    )

    anchors_by_story: dict[int, list[EventAnchors]] = defaultdict(list)
    distances_by_story: dict[int, list[float]] = defaultdict(list)
    for story_id, member_id, title, description, body_text in members:
        anchors_by_story[story_id].append(_anchors(title, description, body_text))
        if distances.get(member_id) is not None:
            distances_by_story[story_id].append(float(distances[member_id]))
    return {
        story_id: CandidateEvidence(
            nearest_member_distance=min(distances_by_story[story_id], default=None),
            members_checked=len(anchors_by_story[story_id]),
            shared_anchors=shared_anchor_count(article, anchors_by_story[story_id]),
        )
        for story_id in story_ids
    }
