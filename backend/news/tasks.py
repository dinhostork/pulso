"""Celery adapters for News ingestion and Story processing (ADR-0001, ADR-0005).

Every task here is a thin orchestration adapter: it resolves identifiers,
invokes one application function and translates the operational metadata that
function returns into Celery scheduling. No normalization, canonicalization,
identity or deduplication rule lives in this module — those belong to
`news.domain` and `news.application` (#16, #17, #18).

Retry responsibilities are layered:

```text
Fetcher      → whether a transport failure is retryable, and any Retry-After
application  → records the run and returns actual retry metadata
this module  → decides when an allowed retry is scheduled
Celery       → performs the future execution
```
"""

import random
from datetime import timedelta
from functools import partial

from celery import shared_task
from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from news.application.ingest import RunSummary
from news.application.ingest import ingest_endpoint as ingest_endpoint_app
from news.application.operations import prune_ingestion_runs as prune_ingestion_runs_app
from news.application.ports import FetchErrorKind
from news.application.process import ProcessOutcome
from news.application.process import process_raw_article as process_raw_article_app
from news.application.story_processing import (
    StepResult,
    claim_for_reconciliation,
    embed_step,
    match_step,
    reconciliation_candidates,
)
from news.logging import ingestion_logger
from news.models import Article, ArticleStoryProcessing, IngestionRun, RawArticle, SourceEndpoint

TASK_LOGGER = "pulso.news.tasks"

TRIGGER_SCHEDULE = "SCHEDULE"
TRIGGER_MANUAL = "MANUAL"
TRIGGER_RETRY = "RETRY"

# Exponential backoff for retryable ingestion failures: 30 s · 2^n, bounded.
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 600
RETRY_JITTER_RATIO = 0.1
# A fetch plus one batch of processing needs more than the global 30 s/60 s
# defaults; these limits apply to this task only (config/common.py keeps the
# global limits for everything else).
INGEST_SOFT_TIME_LIMIT_SECONDS = 150
INGEST_TIME_LIMIT_SECONDS = 180
# Pending rows this old were missed by their own run's processing loop.
PENDING_RECONCILE_AFTER_SECONDS = 600
PENDING_RECONCILE_LIMIT = 1000


def _jitter(spread: float) -> float:
    """Isolated randomness seam; retry-schedule tests replace this function."""

    return random.uniform(0, spread)


def _backoff_seconds(retries: int) -> int:
    """30 · 2^retries plus up to 10% jitter, never above RETRY_MAX_SECONDS.

    Jitter is additive and bounded to a tenth of the step, so consecutive
    countdowns stay strictly increasing until the cap is reached.
    """

    base = min(RETRY_BASE_SECONDS * 2**retries, RETRY_MAX_SECONDS)
    return int(min(RETRY_MAX_SECONDS, base + _jitter(base * RETRY_JITTER_RATIO)))


def _retry_countdown(summary: RunSummary, retries: int) -> int:
    """Honor a server-provided delay for rate limiting; otherwise back off.

    `Retry-After` is a directive from the upstream server, so it is used as
    given rather than replaced by our own backoff. The fetcher already parses
    and bounds it (to 900 s), which is why it may legitimately exceed the
    RETRY_MAX_SECONDS cap that applies to our own exponential schedule.
    Nonsensical values fall back to the ordinary backoff.
    """

    if (
        summary.error_kind == FetchErrorKind.RATE_LIMITED
        and summary.retry_after is not None
        and summary.retry_after >= 0
    ):
        return summary.retry_after
    return _backoff_seconds(retries)


def _run_payload(summary: RunSummary) -> dict:
    """JSON-serializable result; the broker never carries domain objects."""

    return {
        "run_id": summary.run_id,
        "status": str(summary.status),
        "will_retry": summary.will_retry,
        "error_kind": summary.error_kind,
    }


def _outcome_payload(outcome: ProcessOutcome) -> dict:
    return {
        "raw_id": outcome.raw_id,
        "state": str(outcome.state),
        "outcome": str(outcome.outcome),
        "rejection_reason": outcome.rejection_reason,
        "article_id": outcome.article_id,
    }


@shared_task(
    bind=True,
    max_retries=3,
    soft_time_limit=INGEST_SOFT_TIME_LIMIT_SECONDS,
    time_limit=INGEST_TIME_LIMIT_SECONDS,
)
def ingest_endpoint(self, endpoint_id: int, *, trigger: str = TRIGGER_SCHEDULE) -> dict:
    """Ingest one endpoint and translate the run's retry metadata into Celery.

    The task never interprets HTTP status codes or exceptions itself: it
    retries exactly when the application says a retry is warranted and still
    permitted by this task's budget.
    """

    attempt = self.request.retries
    summary = ingest_endpoint_app(
        endpoint_id,
        trigger=TRIGGER_RETRY if attempt else trigger,
        attempt=attempt,
        task_id=self.request.id or "",
        retry_allowed=attempt < self.max_retries,
    )
    if not summary.will_retry:
        return _run_payload(summary)

    countdown = _retry_countdown(summary, attempt)
    ingestion_logger(
        TASK_LOGGER,
        endpoint_id=endpoint_id,
        run_id=summary.run_id,
        task_id=self.request.id or "",
        attempt=attempt,
        trigger=TRIGGER_RETRY if attempt else trigger,
    ).warning(
        "News ingestion retry scheduled",
        extra={
            "countdown": countdown,
            "error_kind": summary.error_kind,
            "retry_after": summary.retry_after,
        },
    )
    raise self.retry(countdown=countdown)


@shared_task(bind=True, max_retries=3)
def process_raw_article(self, raw_id: int) -> dict:
    """Process one stored revision, retrying only transient database failures.

    Deterministic results — normalization rejections, identity conflicts,
    LOCKED and SKIPPED — are final and never retried.
    """

    try:
        outcome = process_raw_article_app(raw_id)
    except OperationalError as error:
        attempt = self.request.retries
        ingestion_logger(TASK_LOGGER, task_id=self.request.id or "", attempt=attempt).warning(
            "News raw processing database error",
            extra={
                "raw_article_id": raw_id,
                "exception_class": type(error).__name__,
            },
        )
        raise self.retry(countdown=_backoff_seconds(attempt), exc=error) from error
    return _outcome_payload(outcome)


def _latest_run_subquery(field: str) -> Subquery:
    """One correlated lookup per field instead of a query per endpoint."""

    return Subquery(
        IngestionRun.objects.filter(endpoint=OuterRef("pk"))
        .order_by("-started_at", "-pk")
        .values(field)[:1]
    )


@shared_task
def poll_due_endpoints() -> dict:
    """Dispatch ingestion for every active, due endpoint of an active Source.

    `skipped_inactive` counts endpoints that are themselves inactive or whose
    Source is inactive; such an endpoint is never dispatched, even when the
    other one of the pair is active. `skipped_running` counts endpoints whose
    latest run is RUNNING and younger than NEWS_STALE_RUNNING_SECONDS; an older
    RUNNING row does not block the endpoint once its interval has elapsed.
    Dispatch happens only after the selecting transaction commits.
    """

    now = timezone.now()
    running_cutoff = now - timedelta(seconds=settings.NEWS_STALE_RUNNING_SECONDS)
    counts = {"dispatched": 0, "skipped_not_due": 0, "skipped_running": 0, "skipped_inactive": 0}

    with transaction.atomic():
        endpoints = (
            SourceEndpoint.objects.select_related("source")
            .annotate(
                latest_started_at=_latest_run_subquery("started_at"),
                latest_status=_latest_run_subquery("status"),
            )
            .order_by("pk")
        )
        for endpoint in endpoints:
            if not endpoint.is_active or not endpoint.source.is_active:
                counts["skipped_inactive"] += 1
                continue
            started_at = endpoint.latest_started_at
            if (
                endpoint.latest_status == IngestionRun.Status.RUNNING
                and started_at is not None
                and started_at > running_cutoff
            ):
                counts["skipped_running"] += 1
                continue
            due_at = (
                None
                if started_at is None
                else started_at + timedelta(seconds=endpoint.fetch_interval_seconds)
            )
            if due_at is not None and due_at > now:
                counts["skipped_not_due"] += 1
                continue
            counts["dispatched"] += 1
            # partial() binds this endpoint id now; a closure over the loop
            # variable would dispatch the last endpoint repeatedly.
            transaction.on_commit(
                partial(ingest_endpoint.delay, endpoint.pk, trigger=TRIGGER_SCHEDULE)
            )

    ingestion_logger(TASK_LOGGER).info("News due-endpoint poll completed", extra=dict(counts))
    return counts


@shared_task
def prune_ingestion_runs() -> dict:
    """Apply the run-history retention rule weekly.

    The rule lives in the application layer, shared with the operator command
    `manage.py news_prune_runs`; this task only invokes it.
    """

    return {"deleted": prune_ingestion_runs_app()}


@shared_task
def reconcile_pending_raw_articles() -> dict:
    """Re-dispatch processing for revisions their own run left PENDING.

    Bounded to PENDING_RECONCILE_LIMIT rows in deterministic receipt order.
    Receipt provenance (payload, hash, endpoint, run, timestamps) is never
    touched: only processing is retried, through the same application function
    the ingestion loop uses.
    """

    cutoff = timezone.now() - timedelta(seconds=PENDING_RECONCILE_AFTER_SECONDS)
    with transaction.atomic():
        raw_ids = list(
            RawArticle.objects.filter(status=RawArticle.Status.PENDING, created_at__lte=cutoff)
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)[:PENDING_RECONCILE_LIMIT]
        )
        for raw_id in raw_ids:
            transaction.on_commit(partial(process_raw_article.delay, raw_id))

    ingestion_logger(TASK_LOGGER).info(
        "News pending reconciliation completed",
        extra={"dispatched": len(raw_ids), "limit": PENDING_RECONCILE_LIMIT},
    )
    return {"dispatched": len(raw_ids)}


# --- Story processing (#29) --------------------------------------------------
#
# The same layering as ingestion: `news.application.story_processing` embeds,
# matches, records state and classifies each failure as retryable or not; these
# tasks only schedule the retries it allows. Arguments are Article ids — never
# models, text or vectors. Redelivery is safe because both steps are idempotent.

# Embedding may run a local model on the CPU; matching is one retrieval query
# plus a short transaction.
STORY_EMBED_SOFT_TIME_LIMIT_SECONDS = 120
STORY_EMBED_TIME_LIMIT_SECONDS = 150
STORY_MATCH_SOFT_TIME_LIMIT_SECONDS = 30
STORY_MATCH_TIME_LIMIT_SECONDS = 60
STORY_RECONCILE_SOFT_TIME_LIMIT_SECONDS = 60
STORY_RECONCILE_TIME_LIMIT_SECONDS = 90


def _step_payload(result: StepResult) -> dict:
    return {
        "article_id": result.article_id,
        "state": str(result.state),
        "pipeline_key": result.pipeline_key,
        "attempts": result.attempts,
        "error_kind": result.error_kind,
        "story_id": result.story_id,
    }


def _run_story_step(task, step, article_id: int, message: str) -> dict:
    """Run one step and translate its verdict into Celery scheduling."""

    attempt = task.request.retries
    logger = ingestion_logger(TASK_LOGGER, task_id=task.request.id or "", attempt=attempt)
    try:
        result = step(article_id)
    except Article.DoesNotExist:
        logger.warning(message + " skipped", extra={"article_id": article_id, "state": "MISSING"})
        return {"article_id": article_id, "state": "MISSING"}
    except OperationalError as error:
        # The failure could not even be recorded; retry like raw processing.
        raise task.retry(countdown=_backoff_seconds(attempt), exc=error) from error
    payload = _step_payload(result)
    if result.state == ArticleStoryProcessing.State.FAILED:
        will_retry = result.retryable and attempt < task.max_retries
        logger.warning(
            message + " failed",
            extra={
                **payload,
                "will_retry": will_retry,
                "countdown": _backoff_seconds(attempt) if will_retry else None,
            },
        )
        if will_retry:
            raise task.retry(countdown=_backoff_seconds(attempt))
        return payload
    logger.info(message + " completed", extra=payload)
    return payload


@shared_task(
    bind=True,
    max_retries=3,
    soft_time_limit=STORY_EMBED_SOFT_TIME_LIMIT_SECONDS,
    time_limit=STORY_EMBED_TIME_LIMIT_SECONDS,
)
def embed_article_story(self, article_id: int) -> dict:
    """Embed one Article, then hand it to matching once the embedding exists."""

    payload = _run_story_step(self, embed_step, article_id, "News Story embedding")
    if payload.get("state") == ArticleStoryProcessing.State.EMBEDDED:
        match_article_story.delay(article_id)
    return payload


@shared_task(
    bind=True,
    max_retries=3,
    soft_time_limit=STORY_MATCH_SOFT_TIME_LIMIT_SECONDS,
    time_limit=STORY_MATCH_TIME_LIMIT_SECONDS,
)
def match_article_story(self, article_id: int) -> dict:
    """Assign one embedded Article to its Story."""

    return _run_story_step(self, match_step, article_id, "News Story matching")


@shared_task(
    soft_time_limit=STORY_RECONCILE_SOFT_TIME_LIMIT_SECONDS,
    time_limit=STORY_RECONCILE_TIME_LIMIT_SECONDS,
)
def reconcile_article_stories() -> dict:
    """Re-dispatch a bounded batch of missing, stale, stuck or retryable Articles.

    Selection and bookkeeping live in the application layer; dispatch happens
    only after the selecting transaction commits.
    """

    with transaction.atomic():
        article_ids = reconciliation_candidates()
        claim_for_reconciliation(article_ids)
        for article_id in article_ids:
            transaction.on_commit(partial(embed_article_story.delay, article_id))

    ingestion_logger(TASK_LOGGER).info(
        "News Story reconciliation completed",
        extra={"dispatched": len(article_ids), "limit": settings.NEWS_STORY_RECONCILE_BATCH},
    )
    return {"dispatched": len(article_ids)}
