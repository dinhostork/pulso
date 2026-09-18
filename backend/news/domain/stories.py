"""Pure Story values shared by candidate retrieval and matching; no Django or models."""

from dataclasses import dataclass
from datetime import datetime

# Mirrors `news.models.Story.Status` without importing the ORM.
STORY_ACTIVE = "ACTIVE"
STORY_ARCHIVED = "ARCHIVED"


def language_key(language: str) -> str:
    """Primary language subtag, case-folded: `en`, `en-GB` and `EN` are compatible."""

    return language.strip().split("-", 1)[0].lower()


@dataclass(frozen=True)
class StoryCandidate:
    """One plausible existing Story for an Article; identifiers and scores only.

    `distance` is the pgvector cosine distance between the Article's and the
    Story's embeddings under one `model_key` (0 identical, 2 opposite).
    `last_article_published_at` is the latest member Article's publication
    time (`first_seen_at` when a member has no `published_at`), never the
    Story's creation or ingestion time.
    """

    story_id: int
    distance: float
    member_count: int
    last_article_published_at: datetime
    language: str
    status: str = STORY_ACTIVE
