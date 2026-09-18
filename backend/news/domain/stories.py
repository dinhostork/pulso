"""Pure Story values shared by retrieval, matching and refresh; no Django or models."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

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


def member_signature(members: Iterable[tuple[int, datetime]]) -> str:
    """Deterministic identity of a Story's membership and its members' revisions.

    `members` are (article_id, revision marker) pairs; the marker is
    `Article.updated_at`, which News Core moves whenever a new revision of the
    Article is applied. The pairs are sorted by article id, each rendered as
    `<id>:<UTC ISO-8601 marker>` on its own line, and hashed with SHA-256, so
    the signature is independent of input order and identical in every
    process. Adding, removing or revising a member changes it.
    """

    lines = sorted(
        (article_id, marker.astimezone(UTC).isoformat()) for article_id, marker in members
    )
    body = "\n".join(f"{article_id}:{marker}" for article_id, marker in lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
