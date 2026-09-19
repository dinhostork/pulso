"""Refresh one coherent Story generation with snapshot, compute and CAS (#32).

Each refresh emits one `story_refresh` record with its outcome (#33), plus one
record per computed component. A failure is kept on the Story as
`refresh_error = "<step>:<error kind>"`, where step is one of
`FAILURE_STEPS`, so an operator can tell which component failed without logs.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.conf import settings
from django.db import OperationalError, transaction
from django.utils import timezone

from news.application.embeddings import (
    ComputedStoryEmbedding,
    compute_story_embedding,
    configured_provider,
)
from news.application.story_enrichment import (
    ComputedEnrichment,
    compute_story_enrichment,
    persist_story_enrichment,
    story_text_from_members,
)
from news.application.story_synthesis import (
    ComputedSynthesis,
    compute_story_synthesis,
    persist_story_synthesis,
    synthesis_input_from_members,
)
from news.domain.embeddings import story_vector
from news.domain.stories import member_signature
from news.logging import ContextLoggerAdapter, log_step, story_logger
from news.models import (
    Article,
    ArticleEmbedding,
    Story,
    StoryArticle,
    StoryEmbedding,
    StorySynthesis,
)

#: Where a refresh can fail; recorded as the prefix of `Story.refresh_error`.
FAILURE_STEPS = ("story_embedding", "story_enrichment", "story_synthesis", "refresh_promotion")


@dataclass(frozen=True)
class RefreshMember:
    article_id: int
    updated_at: datetime
    source_id: int
    source_slug: str
    title: str
    description: str
    body_text: str
    event_time: datetime


@dataclass(frozen=True)
class StoryRefreshSnapshot:
    story_id: int
    members: tuple[RefreshMember, ...]
    signature: str
    article_count: int
    source_count: int
    first_published_at: datetime | None
    last_published_at: datetime | None
    current: bool


@dataclass(frozen=True)
class ComputedStoryRefresh:
    signature: str
    embedding: ComputedStoryEmbedding
    enrichment: ComputedEnrichment
    synthesis: ComputedSynthesis


class RefreshOutcome(StrEnum):
    REFRESHED = "REFRESHED"
    NOOP = "NOOP"
    STALE_RETRY = "STALE_RETRY"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RefreshResult:
    story_id: int
    outcome: RefreshOutcome
    member_signature: str
    article_count: int
    source_count: int
    reason: str
    retryable: bool = False
    error_kind: str = ""
    failed_step: str = ""


def _pairs(story_id: int):
    return Article.objects.filter(story_articles__story_id=story_id).values_list("pk", "updated_at")


def snapshot_story(story_id: int) -> StoryRefreshSnapshot:
    """Read the full membership in one short, read-only transaction."""

    with transaction.atomic():
        story = Story.objects.only("pk", "member_signature", "refresh_state").get(pk=story_id)
        rows = Article.objects.filter(story_articles__story_id=story_id).values_list(
            "pk",
            "updated_at",
            "source_id",
            "source__slug",
            "title",
            "description",
            "body_text",
            "published_at",
            "first_seen_at",
        )
        members = tuple(
            RefreshMember(
                pk, revision, source_id, slug, title, description, body, published or seen
            )
            for pk, revision, source_id, slug, title, description, body, published, seen in rows
        )
        signature = member_signature((member.article_id, member.updated_at) for member in members)
        times = [member.event_time for member in members]
        return StoryRefreshSnapshot(
            story_id,
            members,
            signature,
            len(members),
            len({member.source_id for member in members}),
            min(times) if times else None,
            max(times) if times else None,
            story.refresh_state == Story.RefreshState.CURRENT
            and story.member_signature == signature,
        )


def _dispatch(story_id: int, reason: str) -> None:
    from news.tasks import refresh_story_task

    refresh_story_task.delay(story_id, reason=reason[:64])


def schedule_refresh(story_id: int, *, reason: str) -> None:
    """Queue only after commit. Named callback supports Django's robust logging."""

    if not settings.NEWS_STORY_PROCESSING_ENABLED:
        return

    def dispatch_story_refresh() -> None:
        _dispatch(story_id, reason)

    transaction.on_commit(dispatch_story_refresh, robust=True)


def mark_story_stale(story_id: int, *, reason: str) -> None:
    """Call inside the membership or Article revision transaction."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Story invalidation requires a transaction")
    Story.objects.filter(pk=story_id).update(
        refresh_state=Story.RefreshState.STALE, refresh_error=""
    )
    schedule_refresh(story_id, reason=reason)


def withdraw_member_vector(story_id: int) -> None:
    """Call inside the transaction that removed a member from the Story (#38).

    Until the refresh runs, the Story vector still averages in the removed
    Article, so re-matching that Article compared it with a vector it is part
    of: reconciliation and reprocessing could never undo a merge. Each Story
    vector is rebuilt here as the `story_vector` mean of the remaining members'
    stored embeddings for its model and left unstamped (`member_signature`
    NULL). The refresh this removal queued replaces it with the vector of the
    members' current text. Without a stored embedding for every remaining
    member there is no faithful vector, so it is removed and the Story is not
    a candidate until refreshed. An emptied Story is never a candidate and is
    left to the refresh to archive.
    """

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Story vector withdrawal requires a transaction")
    # Story row first, then its vectors: the order the refresh promotion locks in.
    list(Story.objects.select_for_update().filter(pk=story_id).values_list("pk"))
    members = list(
        StoryArticle.objects.filter(story_id=story_id).values_list("article_id", flat=True)
    )
    if not members:
        return
    for embedding in StoryEmbedding.objects.select_for_update().filter(story_id=story_id):
        vectors = list(
            ArticleEmbedding.objects.filter(
                article_id__in=members, model_key=embedding.model_key
            ).values_list("vector", flat=True)
        )
        if len(vectors) != len(members):
            embedding.delete()
            continue
        embedding.vector = list(story_vector(vectors))
        embedding.member_count = len(vectors)
        embedding.member_signature = None
        embedding.generated_at = timezone.now()
        embedding.save()


def _result(
    snapshot: StoryRefreshSnapshot, outcome: RefreshOutcome, reason: str, **extra
) -> RefreshResult:
    return RefreshResult(
        snapshot.story_id,
        outcome,
        snapshot.signature,
        snapshot.article_count,
        snapshot.source_count,
        reason[:64],
        **extra,
    )


def _changed(snapshot: StoryRefreshSnapshot, story: Story, reason: str) -> RefreshResult | None:
    if member_signature(_pairs(story.pk)) == snapshot.signature:
        return None
    story.refresh_state = Story.RefreshState.STALE
    story.refresh_error = ""
    story.save(update_fields=["refresh_state", "refresh_error", "updated_at"])
    schedule_refresh(story.pk, reason="signature_changed")
    return _result(snapshot, RefreshOutcome.STALE_RETRY, reason)


def _failure(
    snapshot: StoryRefreshSnapshot, error: Exception, reason: str, step: str
) -> RefreshResult:
    retryable = isinstance(error, OperationalError) or bool(getattr(error, "retryable", False))
    kind = str(getattr(error, "kind", type(error).__name__))[:32]
    with transaction.atomic():
        story = Story.objects.select_for_update().get(pk=snapshot.story_id)
        changed = _changed(snapshot, story, reason)
        if changed is not None:
            return changed
        if (
            not snapshot.current
            and story.refresh_state == Story.RefreshState.CURRENT
            and story.member_signature == snapshot.signature
        ):
            return _result(snapshot, RefreshOutcome.NOOP, reason)
        story.refresh_state = Story.RefreshState.FAILED
        story.refresh_error = f"{step}:{kind}"[:512]
        story.save(update_fields=["refresh_state", "refresh_error", "updated_at"])
    return _result(
        snapshot,
        RefreshOutcome.FAILED,
        reason,
        retryable=retryable,
        error_kind=kind,
        failed_step=step,
    )


_REFRESH_STATE = {
    RefreshOutcome.REFRESHED: Story.RefreshState.CURRENT,
    RefreshOutcome.ARCHIVED: Story.RefreshState.CURRENT,
    RefreshOutcome.STALE_RETRY: Story.RefreshState.STALE,
    RefreshOutcome.FAILED: Story.RefreshState.FAILED,
}


def _log_refresh(log: ContextLoggerAdapter, result: RefreshResult, started: float) -> None:
    """One record per refresh, from the application outcome only."""

    fields = {
        "outcome": str(result.outcome),
        "refresh_reason": result.reason,
        "member_count": result.article_count,
        "article_count": result.article_count,
        "source_count": result.source_count,
    }
    state = _REFRESH_STATE.get(result.outcome)
    if state is not None:
        # NOOP changes nothing, so it states no refresh_state of its own.
        fields["refresh_state"] = str(state)
    if result.outcome == RefreshOutcome.FAILED:
        fields.update(error_kind=result.error_kind, failed_step=result.failed_step)
    log_step(
        log, "story_refresh", started, failed=result.outcome == RefreshOutcome.FAILED, **fields
    )


def refresh_story(
    story_id: int,
    *,
    reason: str = "operator",
    provider=None,
    topic_extractor=None,
    entity_extractor=None,
    synthesizer=None,
    logger: ContextLoggerAdapter | None = None,
) -> RefreshResult:
    """Compute without writes or locks; promote one complete generation under CAS."""

    started = time.monotonic()
    log = (logger or story_logger()).bind(story_id=story_id)
    result = _refresh(
        story_id,
        reason=reason,
        provider=provider,
        topic_extractor=topic_extractor,
        entity_extractor=entity_extractor,
        synthesizer=synthesizer,
        log=log,
    )
    _log_refresh(log, result, started)
    return result


def _refresh(
    story_id: int,
    *,
    reason: str,
    provider,
    topic_extractor,
    entity_extractor,
    synthesizer,
    log: ContextLoggerAdapter,
) -> RefreshResult:
    snapshot = snapshot_story(story_id)
    if snapshot.current and (
        not snapshot.members
        or (
            StoryEmbedding.objects.filter(story_id=story_id).exists()
            and StorySynthesis.objects.filter(story_id=story_id, is_current=True).exists()
        )
    ):
        return _result(snapshot, RefreshOutcome.NOOP, reason)

    computed = None
    if snapshot.members:
        step = "story_embedding"
        try:
            provider = provider or configured_provider()
            step_started = time.monotonic()
            try:
                embedding = compute_story_embedding(snapshot.members, provider)
            except Exception as error:
                log_step(
                    log,
                    step,
                    step_started,
                    failed=True,
                    model_key=provider.identity.model_key,
                    error_kind=str(getattr(error, "kind", type(error).__name__))[:32],
                )
                raise
            log_step(
                log,
                step,
                step_started,
                model_key=embedding.model_key,
                member_count=embedding.member_count,
            )
            # Enrichment and synthesis emit their own step records.
            step = "story_enrichment"
            enrichment = compute_story_enrichment(
                story_text_from_members(story_id, snapshot.members),
                topic_extractor=topic_extractor,
                entity_extractor=entity_extractor,
                logger=log,
            )
            step = "story_synthesis"
            synthesis = compute_story_synthesis(
                synthesis_input_from_members(story_id, snapshot.members),
                synthesizer=synthesizer,
                logger=log,
            )
            computed = ComputedStoryRefresh(snapshot.signature, embedding, enrichment, synthesis)
        except Exception as error:
            return _failure(snapshot, error, reason, step)

    try:
        with transaction.atomic():
            story = Story.objects.select_for_update().get(pk=story_id)
            changed = _changed(snapshot, story, reason)
            if changed is not None:
                return changed
            if (
                not snapshot.current
                and story.refresh_state == Story.RefreshState.CURRENT
                and story.member_signature == snapshot.signature
            ):
                return _result(snapshot, RefreshOutcome.NOOP, reason)
            if computed is None:
                story.status = Story.Status.ARCHIVED
            else:
                embedding, enrichment, synthesis = (
                    computed.embedding,
                    computed.enrichment,
                    computed.synthesis,
                )
                StoryEmbedding.objects.update_or_create(
                    story_id=story_id,
                    model_key=embedding.model_key,
                    defaults={
                        "dimension": embedding.dimension,
                        "vector": list(embedding.vector),
                        "member_count": embedding.member_count,
                        "member_signature": snapshot.signature,
                        "generated_at": timezone.now(),
                    },
                )
                persist_story_enrichment(story_id, enrichment, snapshot.signature)
                persist_story_synthesis(story_id, snapshot.signature, synthesis)
            story.member_signature = snapshot.signature
            story.article_count = snapshot.article_count
            story.source_count = snapshot.source_count
            story.first_published_at = snapshot.first_published_at
            story.last_published_at = snapshot.last_published_at
            story.refresh_state = Story.RefreshState.CURRENT
            story.refreshed_at = timezone.now()
            story.refresh_error = ""
            story.save()
    except Exception as error:
        return _failure(snapshot, error, reason, "refresh_promotion")
    return _result(
        snapshot, RefreshOutcome.ARCHIVED if computed is None else RefreshOutcome.REFRESHED, reason
    )


def refresh_candidates(*, limit: int) -> list[int]:
    """Stable bounded operator selection."""

    return list(
        Story.objects.filter(
            refresh_state__in=[Story.RefreshState.STALE, Story.RefreshState.FAILED]
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
