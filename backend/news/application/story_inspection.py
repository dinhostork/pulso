"""Read-only operator views of Story state, built from persisted evidence (#33).

Nothing here decides, retrieves or computes: an Article's explanation is read
from the `StoryArticle.evidence` recorded when the matcher decided (#28), never
by running retrieval again, because a replay could answer differently once
other Stories exist. Every listing is bounded, ordered in SQL and returns
identifiers, states, counts and keys; the only publication fields exposed are
an Article's title and canonical URL, which operators may see. Body text,
description, payload and synthesis text never leave this module.
"""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce

from news.application.story_processing import failed_step, is_fresh
from news.application.story_synthesis import ordered_elements
from news.domain.story_matching import MatchKind, MatchReason, MatchRule
from news.models import (
    ArticleEmbedding,
    ArticleStoryProcessing,
    Story,
    StoryArticle,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElementSource,
    StoryTopic,
)

MAX_LISTING = 1000


def _bounded(value: int, name: str) -> int:
    if not 1 <= value <= MAX_LISTING:
        raise ValueError(f"{name} must be between 1 and {MAX_LISTING}")
    return value


def split_refresh_error(value: str) -> tuple[str, str]:
    """`<step>:<kind>` as recorded by the refresh (#33); older rows hold a kind only."""

    step, separator, kind = value.partition(":")
    return (step, kind) if separator else ("", value)


# --- recent Stories -------------------------------------------------------------------


@dataclass(frozen=True)
class StoryRow:
    story_id: int
    status: str
    language: str
    article_count: int
    source_count: int
    refresh_state: str
    refreshed_at: datetime | None
    refresh_error: str
    synthesis_model_key: str
    created_at: datetime


def recent_stories(*, last: int, refresh_state: str | None = None) -> list[StoryRow]:
    """Newest Stories first (`created_at`, then id), limited in SQL."""

    _bounded(last, "last")
    current_model = StorySynthesis.objects.filter(story=OuterRef("pk"), is_current=True).values(
        "model_key"
    )[:1]
    stories = Story.objects.annotate(synthesis_model_key=Subquery(current_model)).order_by(
        "-created_at", "-pk"
    )
    if refresh_state is not None:
        stories = stories.filter(refresh_state=refresh_state)
    return [
        StoryRow(
            story_id=story.pk,
            status=story.status,
            language=story.language,
            article_count=story.article_count,
            source_count=story.source_count,
            refresh_state=story.refresh_state,
            refreshed_at=story.refreshed_at,
            refresh_error=story.refresh_error,
            synthesis_model_key=story.synthesis_model_key or "",
            created_at=story.created_at,
        )
        for story in stories[:last]
    ]


# --- one Story ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    article_id: int
    source_slug: str
    canonical_url: str
    title: str
    event_time: datetime
    method: str
    is_primary: bool
    distance: float | None
    associated_at: datetime
    cited: bool


@dataclass(frozen=True)
class ElementProvenance:
    kind: str
    position: int
    article_ids: tuple[int, ...]


@dataclass(frozen=True)
class SynthesisProvenance:
    synthesis_id: int
    model_key: str
    member_signature: str
    generated_at: datetime
    elements: tuple[ElementProvenance, ...]

    @property
    def cited_article_ids(self) -> frozenset[int]:
        return frozenset(pk for element in self.elements for pk in element.article_ids)


@dataclass(frozen=True)
class StoryDetail:
    story: Story
    member_total: int
    members: tuple[Member, ...]
    topics: tuple[tuple[str, str, float, str], ...]
    entities: tuple[tuple[str, str, float, str], ...]
    synthesis: SynthesisProvenance | None
    embedding_model_keys: tuple[str, ...]

    @property
    def failed_step(self) -> str:
        return split_refresh_error(self.story.refresh_error)[0]

    @property
    def error_kind(self) -> str:
        return split_refresh_error(self.story.refresh_error)[1]


def _recorded_distance(association) -> float | None:
    distance = (association.evidence or {}).get("distance")
    if association.method == StoryArticle.Method.MATCHED and isinstance(distance, int | float):
        return float(distance)
    if association.similarity is not None:
        return 1.0 - association.similarity
    return None


def _synthesis(story_id: int) -> SynthesisProvenance | None:
    synthesis = StorySynthesis.objects.filter(story_id=story_id, is_current=True).first()
    if synthesis is None:
        return None
    supporters: dict[int, list[int]] = {}
    for element_id, article_id in (
        StorySynthesisElementSource.objects.filter(element__synthesis=synthesis)
        .order_by("element_id", "position")
        .values_list("element_id", "article_id")
    ):
        supporters.setdefault(element_id, []).append(article_id)
    elements = tuple(
        ElementProvenance(kind, position, tuple(supporters.get(element_id, ())))
        for element_id, kind, position in ordered_elements(synthesis.pk).values_list(
            "pk", "kind", "position"
        )
    )
    return SynthesisProvenance(
        synthesis.pk,
        synthesis.model_key,
        synthesis.member_signature,
        synthesis.generated_at,
        elements,
    )


def story_detail(story_id: int, *, member_limit: int) -> StoryDetail:
    """Members in publication order (bounded), Topics, Entities and synthesis provenance."""

    _bounded(member_limit, "member_limit")
    story = Story.objects.get(pk=story_id)
    synthesis = _synthesis(story_id)
    cited = synthesis.cited_article_ids if synthesis else frozenset()
    associations = (
        StoryArticle.objects.filter(story_id=story_id)
        .select_related("article__source")
        # Never load body text, description or anything else not printed.
        .only(
            "article_id",
            "method",
            "is_primary",
            "similarity",
            "evidence",
            "associated_at",
            "article__canonical_url",
            "article__title",
            "article__source__slug",
        )
        .annotate(event_time=Coalesce("article__published_at", "article__first_seen_at"))
        .order_by("event_time", "article_id")
    )
    members = tuple(
        Member(
            article_id=association.article_id,
            source_slug=association.article.source.slug,
            canonical_url=association.article.canonical_url,
            title=association.article.title,
            event_time=association.event_time,
            method=association.method,
            is_primary=association.is_primary,
            distance=_recorded_distance(association),
            associated_at=association.associated_at,
            cited=association.article_id in cited,
        )
        for association in associations[:member_limit]
    )
    topics = tuple(
        StoryTopic.objects.filter(story_id=story_id)
        .order_by("-score", "topic__slug")
        .values_list("topic__slug", "topic__label", "score", "model_key")
    )
    entities = tuple(
        StoryEntity.objects.filter(story_id=story_id)
        .order_by("-score", "entity__kind", "entity__normalized_key")
        .values_list("entity__kind", "entity__display_name", "score", "model_key")
    )
    return StoryDetail(
        story=story,
        member_total=StoryArticle.objects.filter(story_id=story_id).count(),
        members=members,
        topics=topics,
        entities=entities,
        synthesis=synthesis,
        embedding_model_keys=tuple(
            story.embeddings.order_by("model_key").values_list("model_key", flat=True)
        ),
    )


def live_member_counts(story_id: int) -> tuple[int, int]:
    """Current membership (articles, distinct sources), to compare with stored counters."""

    counts = StoryArticle.objects.filter(story_id=story_id).aggregate(
        articles=Count("article_id", distinct=True),
        sources=Count("article__source_id", distinct=True),
    )
    return counts["articles"], counts["sources"]


# --- one Article's explanation ------------------------------------------------------------


@dataclass(frozen=True)
class RecordedCandidate:
    story_id: int
    distance: float


@dataclass(frozen=True)
class RecordedVerification:
    """One secondary-rule check as recorded at decision time (#36)."""

    story_id: int
    distance: float
    result: str
    member_distance: float | None
    members_checked: int
    shared_anchors: int


@dataclass(frozen=True)
class Explanation:
    article_id: int
    processing: ArticleStoryProcessing | None
    fresh: bool
    failed_step: str
    retries_exhausted: bool
    association: StoryArticle | None
    story_status: str
    decision: str
    reason: str
    rule: str
    distance: float | None
    candidate_count: int | None
    candidates: tuple[RecordedCandidate, ...]
    threshold: float | None
    secondary_threshold: float | None
    # Revision 3 (#38) records the member evidence bound separately; earlier
    # evidence has none and shows None, not the candidate bound it then shared.
    secondary_member_threshold: float | None
    kept_current_story: bool
    verifications: tuple[RecordedVerification, ...]
    max_time_gap_hours: float | None
    embedding_model_key: str
    summary: str


def _failed_step(processing: ArticleStoryProcessing) -> str:
    embedded = ArticleEmbedding.objects.filter(
        article_id=processing.article_id, model_key=processing.embedding_model_key
    ).exists()
    return failed_step(processing.error_kind, embedded=embedded)


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def _optional_float(value) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _verifications(evidence: dict) -> tuple[RecordedVerification, ...]:
    return tuple(
        RecordedVerification(
            story_id=int(item["story_id"]),
            distance=float(item["distance"]),
            result=str(item.get("result", "")),
            member_distance=_optional_float(item.get("member_distance")),
            members_checked=int(item.get("members_checked") or 0),
            shared_anchors=int(item.get("shared_anchors") or 0),
        )
        for item in evidence.get("verification", ())
        if isinstance(item, dict) and "story_id" in item and "distance" in item
    )


def _rejections(verifications) -> str:
    return "; ".join(
        f"Story {check.story_id} at {_number(check.distance)}: {check.result} "
        f"(nearest member {_number(check.member_distance)}, "
        f"{check.shared_anchors} shared name(s))"
        for check in verifications
    )


def _summary(explanation: dict, story_id: int) -> str:
    decision, reason = explanation["decision"], explanation["reason"]
    distance, threshold = explanation["distance"], explanation["threshold"]
    secondary = explanation["secondary_threshold"]
    member_bound = explanation["secondary_member_threshold"]
    candidates, verifications = explanation["candidates"], explanation["verifications"]
    if decision == MatchKind.MATCH and explanation["kept_current_story"]:
        return (
            f"stayed in Story {story_id} when its stale assignment was re-decided: the current "
            f"policy still accepts it at distance {_number(distance)} ({reason})"
        )
    if decision == MatchKind.MATCH and reason == MatchReason.VERIFIED_SAME_EVENT:
        accepted = next((c for c in verifications if c.story_id == story_id), None)
        within = f" (member bound {member_bound})" if member_bound is not None else ""
        detail = (
            f"; nearest member at {_number(accepted.member_distance)}{within}, "
            f"{accepted.shared_anchors} shared name(s)"
            if accepted
            else ""
        )
        return (
            f"joined Story {story_id} by the secondary event verifier: distance "
            f"{_number(distance)} is above the primary threshold {threshold} and within the "
            f"secondary bound {secondary}{detail}"
        )
    if decision == MatchKind.MATCH:
        return (
            f"joined Story {story_id}: the nearest compatible candidate was at distance "
            f"{_number(distance)}, within the threshold {threshold}"
        )
    if reason == MatchReason.NO_CANDIDATES:
        return (
            "created a new Story: retrieval returned no candidate (no ACTIVE Story of the same "
            "language and time window within the retrieval distance bound)"
        )
    if reason == MatchReason.NO_COMPATIBLE_CANDIDATE:
        return (
            f"created a new Story: {explanation['candidate_count']} candidate(s) were retrieved "
            f"but none was compatible (ACTIVE, same language, within "
            f"{explanation['max_time_gap_hours']} h of its members)"
        )
    if reason == MatchReason.VERIFICATION_REJECTED:
        return (
            f"created a new Story: no candidate was within the threshold {threshold}, and the "
            f"secondary event verifier rejected every candidate within {secondary}: "
            + _rejections(verifications)
        )
    if reason == MatchReason.ABOVE_THRESHOLD:
        nearest = candidates[0] if candidates else None
        nearest_text = f" (Story {nearest.story_id})" if nearest else ""
        bound = f" and the secondary bound {secondary}" if secondary is not None else ""
        return (
            f"created a new Story: no candidate qualified; the nearest compatible "
            f"candidate{nearest_text} was at distance {_number(distance)}, above the threshold "
            f"{threshold}{bound}"
        )
    return f"decision {decision or '-'} with reason {reason or '-'}"


def explain_article(article_id: int) -> Explanation:
    """The Article's processing state and its primary association's recorded evidence."""

    processing = ArticleStoryProcessing.objects.filter(article_id=article_id).first()
    association = (
        StoryArticle.objects.filter(article_id=article_id, is_primary=True)
        .select_related("story")
        .first()
    )
    fresh = processing is not None and is_fresh(processing)
    failed = processing is not None and processing.state == ArticleStoryProcessing.State.FAILED
    evidence = (association.evidence or {}) if association else {}
    if association is None:
        decision = reason = ""
    elif association.method == StoryArticle.Method.MATCHED:
        decision, reason = str(MatchKind.MATCH), str(evidence.get("reason", ""))
    elif association.method == StoryArticle.Method.CREATED_STORY:
        decision, reason = str(MatchKind.CREATE_NEW_STORY), str(evidence.get("reason", ""))
    else:
        decision, reason = str(association.method), str(evidence.get("reason", ""))
    candidates = tuple(
        RecordedCandidate(int(item["story_id"]), float(item["distance"]))
        for item in evidence.get("candidates", ())
        if isinstance(item, dict) and "story_id" in item and "distance" in item
    )
    count = evidence.get("candidate_count")
    # Revision 1 (#28) recorded no rule: its only match rule was the primary one.
    rule = str(evidence.get("rule") or "")
    if not rule and decision == MatchKind.MATCH and reason == MatchReason.WITHIN_THRESHOLD:
        rule = str(MatchRule.PRIMARY_DISTANCE)
    recorded = {
        "decision": decision,
        "reason": reason,
        "distance": _optional_float(evidence.get("distance")),
        "threshold": _optional_float(evidence.get("max_distance")),
        "secondary_threshold": _optional_float(evidence.get("secondary_max_distance")),
        "secondary_member_threshold": _optional_float(
            evidence.get("secondary_max_member_distance")
        ),
        "kept_current_story": evidence.get("kept_current_story") is True,
        "max_time_gap_hours": _optional_float(evidence.get("max_time_gap_hours")),
        "candidate_count": int(count) if isinstance(count, int) else None,
        "candidates": candidates,
        "verifications": _verifications(evidence),
    }
    if association is None:
        summary = (
            "never attempted: no processing record and no Story association"
            if processing is None
            else "no Story association"
        )
    else:
        summary = _summary(recorded, association.story_id)
    return Explanation(
        article_id=article_id,
        processing=processing,
        fresh=fresh,
        failed_step=_failed_step(processing) if failed else "",
        retries_exhausted=failed
        and processing.attempts >= settings.NEWS_STORY_PROCESSING_MAX_ATTEMPTS,
        association=association,
        story_status=association.story.status if association else "",
        rule=rule,
        embedding_model_key=str(evidence.get("embedding_model_key", "")),
        summary=summary,
        **recorded,
    )
