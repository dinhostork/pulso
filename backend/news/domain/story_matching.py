"""Pure Article -> Story matching decision (#28, #36); no Django, models or settings.

The decision sees only evidence available before enrichment: the semantic
distance of each retrieved candidate, the time between the Article and the
candidate's latest member, language compatibility and, for the secondary rule,
bounded evidence the application gathered from the candidate's members. It
never receives `content_fingerprint`, `duplicate_of`, Source identity or any
Topic/Entity: publication identity (ADR-0010) is not event identity (ADR-0003),
and matching must not depend on enrichment having run.

Rule, applied to candidates re-ordered by (`distance`, `story_id`):

1. A candidate is compatible when it is ACTIVE, shares the Article's primary
   language subtag, and the Article's event time lies within
   `max_time_gap_hours` of the candidate's member publication range
   [first, last] (zero inside it). For a report newer than every member this
   is the gap to the latest member, as in revision 1; the range matters when
   an older report is matched or reprocessed against a Story that has since
   grown, which revision 1 wrongly judged by its newest member. An ARCHIVED
   candidate is treated as absent even if retrieval returned it.
2. Primary rule (`PRIMARY_DISTANCE`): the nearest compatible candidate with
   `distance <= max_distance` is the match.
3. Secondary rule (`SECONDARY_EVENT_VERIFY`, #36): otherwise, every compatible
   candidate with `distance <= secondary_max_distance` is verified in order,
   and the first one verified is the match. Verification needs all of:
   - the anchor rule is defined for the Article's language;
   - the nearest of the candidate's most recent members is itself within
     `secondary_max_distance` of the Article, so a Story vector cannot pull in
     a report that no member resembles;
   - at least `min_anchors` proper names in common
     (`news.domain.event_anchors`).
4. Otherwise a new Story is created: v0.3 still prefers splitting one event
   over merging two, because a merge mixes the facts of distinct events.

Every bound is inclusive. Nothing depends on randomness, the clock or
insertion order.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum

from news.domain.event_anchors import ANCHOR_LANGUAGES
from news.domain.stories import STORY_ACTIVE, StoryCandidate, language_key

POLICY_NAME = "story-match"
# Bump whenever the rule above changes; threshold values are part of the key
# on their own (see `MatchPolicy.matcher_key`). Revision 1 (#28) had only the
# primary rule; revision 2 (#36) adds the secondary verifier and measures the
# time gap to the member range instead of the latest member.
POLICY_REVISION = 2


class MatchKind(StrEnum):
    MATCH = "MATCH"
    CREATE_NEW_STORY = "CREATE_NEW_STORY"


class MatchRule(StrEnum):
    PRIMARY_DISTANCE = "PRIMARY_DISTANCE"
    SECONDARY_EVENT_VERIFY = "SECONDARY_EVENT_VERIFY"


class MatchReason(StrEnum):
    WITHIN_THRESHOLD = "WITHIN_THRESHOLD"
    VERIFIED_SAME_EVENT = "VERIFIED_SAME_EVENT"
    NO_CANDIDATES = "NO_CANDIDATES"
    NO_COMPATIBLE_CANDIDATE = "NO_COMPATIBLE_CANDIDATE"
    ABOVE_THRESHOLD = "ABOVE_THRESHOLD"
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"


class VerificationResult(StrEnum):
    ACCEPTED = "ACCEPTED"
    LANGUAGE_UNSUPPORTED = "LANGUAGE_UNSUPPORTED"
    NO_MEMBER_EVIDENCE = "NO_MEMBER_EVIDENCE"
    MEMBER_TOO_FAR = "MEMBER_TOO_FAR"
    NO_SHARED_ANCHOR = "NO_SHARED_ANCHOR"


@dataclass(frozen=True)
class MatchPolicy:
    """Every number the rule uses; each one is part of `matcher_key`."""

    max_distance: float
    max_time_gap_hours: float
    secondary_max_distance: float
    min_anchors: int
    max_members: int

    def __post_init__(self):
        if not 0 < self.max_distance <= 2:
            raise ValueError("max_distance must be a cosine distance in (0, 2]")
        if not self.max_time_gap_hours > 0:
            raise ValueError("max_time_gap_hours must be positive")
        if not self.max_distance < self.secondary_max_distance <= 2:
            raise ValueError("secondary_max_distance must lie above max_distance, at most 2")
        if self.min_anchors < 1:
            raise ValueError("min_anchors must be at least 1")
        if self.max_members < 1:
            raise ValueError("max_members must be positive")

    @property
    def matcher_key(self) -> str:
        """Stable identity of the policy: name, rule revision and every threshold."""

        # Distances and hours render as the exact, round-tripping float repr and
        # counts as integers, so no two policies share a key.
        values = ";".join(
            f"{field.name}={_key_value(field, getattr(self, field.name))}" for field in fields(self)
        )
        return f"{POLICY_NAME}-v{POLICY_REVISION};{values}"


_INTEGER_FIELDS = frozenset({"min_anchors", "max_members"})


def _key_value(field, value) -> str:
    return str(int(value)) if field.name in _INTEGER_FIELDS else repr(float(value))


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
class CandidateEvidence:
    """What the application measured for one secondary candidate; numbers only.

    `nearest_member_distance` is the smallest cosine distance between the
    Article and one of the candidate's `members_checked` most recent members
    (None when none has an embedding for the model). `shared_anchors` counts
    proper names in common (`news.domain.event_anchors.shared_anchor_count`).
    """

    nearest_member_distance: float | None
    members_checked: int
    shared_anchors: int


@dataclass(frozen=True)
class Verification:
    story_id: int
    distance: float
    result: VerificationResult
    nearest_member_distance: float | None = None
    members_checked: int = 0
    shared_anchors: int = 0


@dataclass(frozen=True)
class MatchDecision:
    kind: MatchKind
    reason: MatchReason
    story_id: int | None = None
    # The deciding distance: the chosen Story's for MATCH, the nearest
    # compatible candidate's for ABOVE_THRESHOLD and VERIFICATION_REJECTED,
    # otherwise None.
    distance: float | None = None
    candidate_count: int = 0
    rule: MatchRule | None = None
    # Every secondary candidate verified, in verification order.
    verifications: tuple[Verification, ...] = ()


def _compatible(
    article: ArticleMatchSnapshot, candidate: StoryCandidate, policy: MatchPolicy
) -> bool:
    return (
        candidate.status == STORY_ACTIVE
        and language_key(candidate.language) == language_key(article.language)
        and candidate.time_gap(article.event_time) <= timedelta(hours=policy.max_time_gap_hours)
    )


def _compatible_ordered(
    article: ArticleMatchSnapshot, candidates: Sequence[StoryCandidate], policy: MatchPolicy
) -> list[StoryCandidate]:
    ordered = sorted(candidates, key=lambda candidate: (candidate.distance, candidate.story_id))
    return [candidate for candidate in ordered if _compatible(article, candidate, policy)]


def secondary_candidates(
    article: ArticleMatchSnapshot, candidates: Sequence[StoryCandidate], policy: MatchPolicy
) -> tuple[StoryCandidate, ...]:
    """The candidates the secondary rule would verify, in order; empty when the
    primary rule already decides. The application gathers evidence for these only."""

    compatible = _compatible_ordered(article, candidates, policy)
    if not compatible or compatible[0].distance <= policy.max_distance:
        return ()
    return tuple(
        candidate for candidate in compatible if candidate.distance <= policy.secondary_max_distance
    )


def verify_candidate(
    article: ArticleMatchSnapshot,
    candidate: StoryCandidate,
    evidence: CandidateEvidence | None,
    policy: MatchPolicy,
) -> Verification:
    """The secondary rule for one candidate; checks in a fixed order."""

    def result(outcome: VerificationResult) -> Verification:
        if evidence is None:
            return Verification(candidate.story_id, candidate.distance, outcome)
        return Verification(
            candidate.story_id,
            candidate.distance,
            outcome,
            evidence.nearest_member_distance,
            evidence.members_checked,
            evidence.shared_anchors,
        )

    if language_key(article.language) not in ANCHOR_LANGUAGES:
        return result(VerificationResult.LANGUAGE_UNSUPPORTED)
    if evidence is None or evidence.nearest_member_distance is None:
        return result(VerificationResult.NO_MEMBER_EVIDENCE)
    if evidence.nearest_member_distance > policy.secondary_max_distance:
        return result(VerificationResult.MEMBER_TOO_FAR)
    if evidence.shared_anchors < policy.min_anchors:
        return result(VerificationResult.NO_SHARED_ANCHOR)
    return result(VerificationResult.ACCEPTED)


def decide_story_match(
    article: ArticleMatchSnapshot,
    candidates: Sequence[StoryCandidate],
    policy: MatchPolicy,
    evidence: Mapping[int, CandidateEvidence] | None = None,
) -> MatchDecision:
    """Join by the primary rule, else by the verified secondary rule, else start a new Story.

    `evidence` maps Story id to what the application measured for the
    candidates `secondary_candidates` returned; a missing entry fails
    verification rather than passing it.
    """

    count = len(candidates)
    if not candidates:
        return MatchDecision(MatchKind.CREATE_NEW_STORY, MatchReason.NO_CANDIDATES)
    compatible = _compatible_ordered(article, candidates, policy)
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
            rule=MatchRule.PRIMARY_DISTANCE,
        )
    evidence = evidence or {}
    verifications = []
    for candidate in secondary_candidates(article, candidates, policy):
        verification = verify_candidate(
            article, candidate, evidence.get(candidate.story_id), policy
        )
        verifications.append(verification)
        if verification.result is VerificationResult.ACCEPTED:
            return MatchDecision(
                MatchKind.MATCH,
                MatchReason.VERIFIED_SAME_EVENT,
                story_id=candidate.story_id,
                distance=candidate.distance,
                candidate_count=count,
                rule=MatchRule.SECONDARY_EVENT_VERIFY,
                verifications=tuple(verifications),
            )
    return MatchDecision(
        MatchKind.CREATE_NEW_STORY,
        MatchReason.VERIFICATION_REJECTED if verifications else MatchReason.ABOVE_THRESHOLD,
        distance=nearest.distance,
        candidate_count=count,
        verifications=tuple(verifications),
    )
