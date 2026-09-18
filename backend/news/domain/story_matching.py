"""Pure Article -> Story matching decision (#28); no Django, models or settings.

The decision sees only evidence available before enrichment: the semantic
distance of each retrieved candidate, the time between the Article and the
candidate's latest member, and language compatibility. It never receives
`content_fingerprint`, `duplicate_of` or Source identity: publication identity
(ADR-0010) is not event identity (ADR-0003).

Rule, applied to candidates re-ordered by (`distance`, `story_id`):

1. A candidate is compatible when it is ACTIVE, shares the Article's primary
   language subtag, and its latest member was published within
   `max_time_gap_hours` of the Article (either direction). An ARCHIVED
   candidate is treated as absent even if retrieval returned it.
2. The nearest compatible candidate with `distance <= max_distance` is the
   match.
3. Otherwise a new Story is created. That includes the ambiguous band — a
   nearest compatible candidate with `max_distance < distance`, still inside
   the recall-oriented retrieval bound: v0.3 prefers splitting one event over
   merging two, because a merge mixes the facts of distinct events.

Nothing depends on randomness, the clock or insertion order.
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum

from news.domain.stories import STORY_ACTIVE, StoryCandidate, language_key

POLICY_NAME = "story-match"
# Bump whenever the rule above changes; threshold values are part of the key
# on their own (see `MatchPolicy.matcher_key`).
POLICY_REVISION = 1


class MatchKind(StrEnum):
    MATCH = "MATCH"
    CREATE_NEW_STORY = "CREATE_NEW_STORY"


class MatchReason(StrEnum):
    WITHIN_THRESHOLD = "WITHIN_THRESHOLD"
    NO_CANDIDATES = "NO_CANDIDATES"
    NO_COMPATIBLE_CANDIDATE = "NO_COMPATIBLE_CANDIDATE"
    ABOVE_THRESHOLD = "ABOVE_THRESHOLD"


@dataclass(frozen=True)
class MatchPolicy:
    """Every number the rule uses; each one is part of `matcher_key`."""

    max_distance: float
    max_time_gap_hours: float

    def __post_init__(self):
        if not 0 < self.max_distance <= 2:
            raise ValueError("max_distance must be a cosine distance in (0, 2]")
        if not self.max_time_gap_hours > 0:
            raise ValueError("max_time_gap_hours must be positive")

    @property
    def matcher_key(self) -> str:
        """Stable identity of the policy: name, rule revision and every threshold."""

        # repr of the float is exact and round-trips, so no two policies share a key.
        values = ";".join(
            f"{field.name}={float(getattr(self, field.name))!r}" for field in fields(self)
        )
        return f"{POLICY_NAME}-v{POLICY_REVISION};{values}"


@dataclass(frozen=True)
class ArticleMatchSnapshot:
    """The incoming Article as the decision sees it.

    `event_time` is `published_at`, or `first_seen_at` when that is missing.
    There is deliberately no fingerprint, `duplicate_of` or Source field.
    """

    article_id: int
    language: str
    event_time: datetime


@dataclass(frozen=True)
class MatchDecision:
    kind: MatchKind
    reason: MatchReason
    story_id: int | None = None
    # The deciding distance: the chosen Story's for MATCH, the nearest
    # compatible candidate's for ABOVE_THRESHOLD, otherwise None.
    distance: float | None = None
    candidate_count: int = 0


def _compatible(
    article: ArticleMatchSnapshot, candidate: StoryCandidate, policy: MatchPolicy
) -> bool:
    gap = abs(article.event_time - candidate.last_article_published_at)
    return (
        candidate.status == STORY_ACTIVE
        and language_key(candidate.language) == language_key(article.language)
        and gap <= timedelta(hours=policy.max_time_gap_hours)
    )


def decide_story_match(
    article: ArticleMatchSnapshot,
    candidates: Sequence[StoryCandidate],
    policy: MatchPolicy,
) -> MatchDecision:
    """Join the nearest compatible Story within the threshold, or start a new one."""

    count = len(candidates)
    if not candidates:
        return MatchDecision(MatchKind.CREATE_NEW_STORY, MatchReason.NO_CANDIDATES)
    ordered = sorted(candidates, key=lambda candidate: (candidate.distance, candidate.story_id))
    compatible = [candidate for candidate in ordered if _compatible(article, candidate, policy)]
    if not compatible:
        return MatchDecision(
            MatchKind.CREATE_NEW_STORY,
            MatchReason.NO_COMPATIBLE_CANDIDATE,
            candidate_count=count,
        )
    nearest = compatible[0]
    if nearest.distance <= policy.max_distance:
        return MatchDecision(
            MatchKind.MATCH,
            MatchReason.WITHIN_THRESHOLD,
            story_id=nearest.story_id,
            distance=nearest.distance,
            candidate_count=count,
        )
    return MatchDecision(
        MatchKind.CREATE_NEW_STORY,
        MatchReason.ABOVE_THRESHOLD,
        distance=nearest.distance,
        candidate_count=count,
    )
