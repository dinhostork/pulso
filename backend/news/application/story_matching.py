"""Match one embedded Article into a Story, or start a new one (#28, #36).

This module only gathers inputs and applies the result: candidates come from
`find_candidates` (#27), secondary-rule evidence from
`news.application.story_verification` (only for the candidates the primary
rule leaves unresolved) and the rule is `news.domain.story_matching`. One
invocation writes at most one Story, one StoryArticle and that new Story's
first StoryEmbedding, all in one transaction, and never touches `Article`,
`RawArticle`, `IngestionRun`, `Source` or `SourceEndpoint`.

Each Article gets at most one *primary* association. That is the v0.3
assignment invariant enforced by #24's partial unique constraint, not a
permanent one-Story-per-Article domain rule: ADR-0003's many-to-many model
stays representable through non-primary associations.

Concurrency follows `news.application.process`: PostgreSQL uniqueness is the
arbiter. A rejected insert rolls back its savepoint, including any Story it
created, and the winner's association is re-read and returned. A chosen Story
found ARCHIVED under its row lock is re-decided once with fresh candidates.
Two same-event Articles matched at the same moment, with no Story yet, each
create a Story. v0.3 accepts that duplicate; the two converge only through
later reprocessing. No lock outside PostgreSQL is used.

`StoryArticle.similarity` is stored as cosine similarity, `1 - distance`, to
the chosen Story, whichever rule accepted it, and is NULL for `CREATED_STORY`.
The raw distance, the rule and every secondary verification are kept in
`evidence` as identifiers, enums and numbers.
"""

import time
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db import IntegrityError, transaction

from news.application.embeddings import configured_provider
from news.application.story_candidates import find_candidates
from news.application.story_refresh import mark_story_stale
from news.application.story_verification import gather_evidence
from news.domain.embeddings import story_vector
from news.domain.stories import StoryCandidate
from news.domain.story_matching import (
    ArticleMatchSnapshot,
    MatchDecision,
    MatchKind,
    MatchPolicy,
    decide_story_match,
    secondary_candidates,
)
from news.logging import ContextLoggerAdapter, log_step, story_logger
from news.models import Article, ArticleEmbedding, Story, StoryArticle, StoryEmbedding

MATCH_POLICY = MatchPolicy(
    max_distance=settings.NEWS_STORY_MATCH_MAX_DISTANCE,
    max_time_gap_hours=settings.NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS,
    secondary_max_distance=settings.NEWS_STORY_MATCH_SECONDARY_MAX_DISTANCE,
    min_anchors=1,
    max_members=settings.NEWS_STORY_MATCH_VERIFY_MAX_MEMBERS,
)
# The matching policy in force. #29 compares it, together with the embedding
# `model_key`, to decide whether an Article's Story assignment is stale.
MATCHER_KEY = MATCH_POLICY.matcher_key
EVIDENCE_CANDIDATES = 5


class MatchState(StrEnum):
    MATCHED = "MATCHED"
    CREATED_STORY = "CREATED_STORY"
    ALREADY_ASSIGNED = "ALREADY_ASSIGNED"


@dataclass(frozen=True)
class MatchOutcome:
    """Plain result values, never a model."""

    article_id: int
    story_id: int
    association_id: int
    state: MatchState
    decision: MatchDecision | None = None


class StoryNoLongerActive(RuntimeError):
    """The chosen Story was archived between retrieval and the write, twice."""


def _existing(article_id: int) -> MatchOutcome | None:
    row = (
        StoryArticle.objects.filter(article_id=article_id, is_primary=True)
        .values_list("pk", "story_id")
        .first()
    )
    if row is None:
        return None
    association_id, story_id = row
    return MatchOutcome(article_id, story_id, association_id, MatchState.ALREADY_ASSIGNED)


def _snapshot(article_id: int) -> ArticleMatchSnapshot:
    published_at, first_seen_at, language = Article.objects.values_list(
        "published_at", "first_seen_at", "language"
    ).get(pk=article_id)
    return ArticleMatchSnapshot(
        article_id=article_id, language=language, event_time=published_at or first_seen_at
    )


def _evidence(
    decision: MatchDecision, candidates: tuple[StoryCandidate, ...], model_key: str
) -> dict:
    """Identifiers, enums and numbers only; bounded by EVIDENCE_CANDIDATES.

    `rule` is the rule that accepted a MATCH (None when a Story was created);
    `verification` lists the secondary candidates checked, in order, with the
    decisive values. Anchor terms are publication text and are never stored,
    only their count.
    """

    return {
        "reason": decision.reason.value,
        "rule": decision.rule.value if decision.rule else None,
        "distance": decision.distance,
        "candidate_count": len(candidates),
        "candidates": [
            {"story_id": candidate.story_id, "distance": candidate.distance}
            for candidate in candidates[:EVIDENCE_CANDIDATES]
        ],
        "verification": [
            {
                "story_id": check.story_id,
                "distance": check.distance,
                "result": check.result.value,
                "member_distance": check.nearest_member_distance,
                "members_checked": check.members_checked,
                "shared_anchors": check.shared_anchors,
            }
            for check in decision.verifications[:EVIDENCE_CANDIDATES]
        ],
        "max_distance": MATCH_POLICY.max_distance,
        "secondary_max_distance": MATCH_POLICY.secondary_max_distance,
        "max_time_gap_hours": MATCH_POLICY.max_time_gap_hours,
        "embedding_model_key": model_key,
    }


def _apply(
    snapshot: ArticleMatchSnapshot,
    model_key: str,
    decision: MatchDecision,
    candidates: tuple[StoryCandidate, ...],
) -> MatchOutcome:
    evidence = _evidence(decision, candidates, model_key)
    if decision.kind is MatchKind.MATCH:
        story = Story.objects.select_for_update().get(pk=decision.story_id)
        if story.status != Story.Status.ACTIVE:
            raise StoryNoLongerActive
        association = StoryArticle.objects.create(
            story=story,
            article_id=snapshot.article_id,
            is_primary=True,
            method=StoryArticle.Method.MATCHED,
            similarity=1.0 - decision.distance,
            matcher_key=MATCHER_KEY,
            evidence=evidence,
        )
        state = MatchState.MATCHED
    else:
        embedding = ArticleEmbedding.objects.get(
            article_id=snapshot.article_id, model_key=model_key
        )
        story = Story.objects.create(status=Story.Status.ACTIVE, language=snapshot.language)
        association = StoryArticle.objects.create(
            story=story,
            article_id=snapshot.article_id,
            is_primary=True,
            method=StoryArticle.Method.CREATED_STORY,
            similarity=None,
            matcher_key=MATCHER_KEY,
            evidence=evidence,
        )
        StoryEmbedding.objects.create(
            story=story,
            model_key=model_key,
            dimension=embedding.dimension,
            vector=list(story_vector([embedding.vector])),
            member_count=1,
        )
        state = MatchState.CREATED_STORY
    mark_story_stale(story.pk, reason="membership_added")
    return MatchOutcome(snapshot.article_id, story.pk, association.pk, state, decision)


def _log_association(
    log: ContextLoggerAdapter, outcome: MatchOutcome, started: float
) -> MatchOutcome:
    log_step(
        log,
        "story_association",
        started,
        outcome=str(outcome.state),
        story_id=outcome.story_id,
        matcher_key=MATCHER_KEY,
    )
    return outcome


def _log_decision(log: ContextLoggerAdapter, decision: MatchDecision, started: float) -> None:
    """The decision as the domain made it; no second taxonomy."""

    log_step(
        log,
        "matching_decision",
        started,
        decision=str(decision.kind),
        match_reason=str(decision.reason),
        match_rule=str(decision.rule) if decision.rule else None,
        chosen_story_id=decision.story_id,
        distance=decision.distance,
        threshold=MATCH_POLICY.max_distance,
        candidate_count=decision.candidate_count,
    )


def match_article(
    article_id: int,
    *,
    model_key: str | None = None,
    logger: ContextLoggerAdapter | None = None,
) -> MatchOutcome:
    """Assign the Article to a Story once; replays return the existing assignment.

    Requires the Article's embedding for `model_key` (default: the configured
    provider's); a missing one raises `MissingArticleEmbedding` before any write.
    Emits one record each for retrieval, decision and association; a replay
    logs only the association it found, since no decision was made.
    """

    started = time.monotonic()
    log = (logger or story_logger()).bind(article_id=article_id)
    existing = _existing(article_id)
    if existing is not None:
        return _log_association(log, existing, started)
    if model_key is None:
        model_key = configured_provider().identity.model_key
    log = log.bind(model_key=model_key)
    snapshot = _snapshot(article_id)
    for attempt in range(2):
        step_started = time.monotonic()
        candidates = find_candidates(article_id, model_key)
        log_step(log, "candidate_retrieval", step_started, candidate_count=len(candidates))
        step_started = time.monotonic()
        evidence = gather_evidence(
            article_id,
            model_key,
            secondary_candidates(snapshot, candidates, MATCH_POLICY),
            MATCH_POLICY,
        )
        decision = decide_story_match(snapshot, candidates, MATCH_POLICY, evidence)
        _log_decision(log, decision, step_started)
        try:
            with transaction.atomic():
                outcome = _apply(snapshot, model_key, decision, candidates)
            return _log_association(log, outcome, started)
        except IntegrityError:
            existing = _existing(article_id)
            if existing is not None:
                return _log_association(log, existing, started)
            if attempt:
                raise
        except StoryNoLongerActive:
            # Retrieval no longer returns the archived Story; decide once more.
            if attempt:
                raise
    raise AssertionError("unreachable")
