"""Persist fetched News material as immutable, replayable RawArticle revisions."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from news.adapters.http import Fetcher
from news.application.ports import EndpointFetchRequest, FetchedItem, FetchError
from news.application.process import ProcessOutcome, ProcessState, process_raw_article
from news.application.registry import adapter_for
from news.domain.fingerprints import payload_hash
from news.domain.identity import ExternalKey, MissingIdentity, external_key
from news.logging import endpoint_context, ingestion_logger, run_context
from news.models import IngestionRun, RawArticle, SourceEndpoint


@dataclass(frozen=True)
class RunSummary:
    """Operational result of one run, including transient retry metadata.

    `will_retry` states whether a retry is both warranted and permitted, so a
    caller can translate it directly into scheduling. `retry_after` carries a
    server-provided delay (seconds) for the current failure and is deliberately
    not persisted: it describes this attempt's scheduling, not run history.
    """

    run_id: int
    status: str
    will_retry: bool
    error_kind: str
    items_received: int
    items_rejected: int
    raw_created: int
    raw_unchanged: int
    raw_changed: int
    items_processed: int
    items_failed: int
    identity_duplicates: int
    content_duplicates: int
    raw_rejected: int
    source_identity_conflicts: int
    retry_after: int | None = None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _stored_payload(item: FetchedItem) -> tuple[dict, bool]:
    payload = {
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "summary_html": item.summary_html,
        "content_html": item.content_html,
        "published_at": _iso(item.published_at),
        "updated_at": _iso(item.updated_at),
        "authors": list(item.authors),
        "language": item.language,
        "raw": dict(item.raw),
        "truncated": False,
    }
    if len(_canonical_bytes(payload)) <= settings.NEWS_INGEST_MAX_PAYLOAD_BYTES:
        return payload, False

    payload.update(
        summary_html=None,
        content_html=None,
        authors=[],
        raw={},
        truncated=True,
    )
    # Preserve bounded prefixes of identity/provenance fields. Identity itself
    # remains lossless in the dedicated RawArticle columns.
    for field in ("title", "url", "external_id", "language"):
        if isinstance(payload[field], str):
            payload[field] = payload[field][:4096]
    if len(_canonical_bytes(payload)) > settings.NEWS_INGEST_MAX_PAYLOAD_BYTES:
        # Fixed-size fields above make this unreachable under ordinary inputs;
        # retain a deterministic minimal valid document as a final guard.
        payload = {
            "external_id": (item.external_id or "")[:4096] or None,
            "url": (item.url or "")[:4096] or None,
            "title": (item.title or "")[:4096] or None,
            "published_at": _iso(item.published_at),
            "updated_at": _iso(item.updated_at),
            "language": (item.language or "")[:256] or None,
            "truncated": True,
        }
    return payload, True


def _summary(run: IngestionRun, *, retry_after: int | None = None) -> RunSummary:
    return RunSummary(
        run_id=run.pk,
        status=run.status,
        will_retry=run.will_retry,
        error_kind=run.error_kind,
        items_received=run.items_received,
        items_rejected=run.items_rejected,
        raw_created=run.raw_created,
        raw_unchanged=run.raw_unchanged,
        raw_changed=run.raw_changed,
        items_processed=run.items_processed,
        items_failed=run.items_failed,
        identity_duplicates=run.identity_duplicates,
        content_duplicates=run.content_duplicates,
        raw_rejected=run.raw_rejected,
        source_identity_conflicts=run.source_identity_conflicts,
        retry_after=retry_after,
    )


def _finalize(
    run: IngestionRun,
    endpoint: SourceEndpoint,
    started: float,
    status: str,
    logger: logging.LoggerAdapter,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    retry_after: int | None = None,
) -> RunSummary:
    run.status = status
    run.finished_at = timezone.now()
    run.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    fields = [
        "status",
        "finished_at",
        "duration_ms",
        "http_status",
        "error_kind",
        "error_message",
        "will_retry",
        "items_received",
        "items_rejected",
        "raw_created",
        "raw_unchanged",
        "raw_changed",
        "items_processed",
        "items_failed",
        "identity_duplicates",
        "content_duplicates",
        "raw_rejected",
        "source_identity_conflicts",
    ]
    with transaction.atomic():
        updates = {}
        if etag:
            updates["etag"] = etag
        if last_modified:
            updates["last_modified"] = last_modified
        if updates:
            SourceEndpoint.objects.filter(pk=endpoint.pk).update(**updates)
        run.save(update_fields=fields)
    logger.info(
        "News ingestion run finalized",
        extra={
            "status": run.status,
            "error_kind": run.error_kind,
            "http_status": run.http_status,
            "will_retry": run.will_retry,
            "duration_ms": run.duration_ms,
            "items_received": run.items_received,
            "items_rejected": run.items_rejected,
            "raw_created": run.raw_created,
            "raw_changed": run.raw_changed,
            "raw_unchanged": run.raw_unchanged,
            "items_processed": run.items_processed,
            "items_failed": run.items_failed,
            "identity_duplicates": run.identity_duplicates,
            "content_duplicates": run.content_duplicates,
            "raw_rejected": run.raw_rejected,
            "source_identity_conflicts": run.source_identity_conflicts,
        },
    )
    return _summary(run, retry_after=retry_after)


def _persist_item(
    endpoint: SourceEndpoint,
    run: IngestionRun,
    identity: ExternalKey,
    item: FetchedItem,
    stored: dict,
    digest: str,
    fetched_at: datetime,
) -> tuple[RawArticle, str]:
    previous = (
        RawArticle.objects.filter(
            endpoint=endpoint,
            external_key_kind=identity.kind.value,
            external_key=identity.value,
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    try:
        with transaction.atomic():
            raw = RawArticle.objects.create(
                endpoint=endpoint,
                ingestion_run=run,
                external_key_kind=identity.kind.value,
                external_key=identity.value,
                external_id=item.external_id or "",
                url=item.url or "",
                payload=stored,
                payload_hash=digest,
                supersedes=previous,
                fetched_at=fetched_at,
            )
        return raw, "changed" if previous else "created"
    except IntegrityError:
        existing = RawArticle.objects.filter(
            endpoint=endpoint,
            external_key_kind=identity.kind.value,
            external_key=identity.value,
            payload_hash=digest,
        ).first()
        if existing is None:
            raise
        return existing, "unchanged"


def _count_processing(run: IngestionRun, outcome: ProcessOutcome) -> None:
    """Maintain the dedup counters from one processing result (ADR-0010).

    `raw_rejected` covers every RawArticle rejected while processing, including
    normalization rejections; adapter/intake rejection keeps its own
    `items_rejected` meaning from #16.
    """

    if outcome.state is ProcessState.REJECTED:
        run.raw_rejected += 1
        if outcome.rejection_reason == RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT:
            run.source_identity_conflicts += 1
    if outcome.outcome == RawArticle.Outcome.IDENTITY_DUPLICATE:
        run.identity_duplicates += 1
    elif outcome.outcome == RawArticle.Outcome.CONTENT_DUPLICATE:
        run.content_duplicates += 1


def ingest_endpoint(
    endpoint_id: int,
    *,
    trigger: str,
    attempt: int = 0,
    task_id: str = "",
    retry_allowed: bool = True,
) -> RunSummary:
    """Fetch one endpoint, persist raw revisions, and process each pending row.

    `retry_allowed` lets a caller that owns the retry budget (the Celery task
    adapter, #19) say that no further attempt will happen, so the persisted run
    records the actual orchestration intent instead of a retry that is never
    scheduled. It never changes fetching, intake or processing behavior.
    """

    endpoint = SourceEndpoint.objects.select_related("source").get(pk=endpoint_id)
    started = time.monotonic()
    with transaction.atomic():
        run = IngestionRun.objects.create(
            endpoint=endpoint,
            trigger=trigger,
            attempt=attempt,
            task_id=task_id,
            started_at=timezone.now(),
        )
    logger = ingestion_logger(**endpoint_context(endpoint), **run_context(run))
    logger.info("News ingestion run started")

    request = EndpointFetchRequest(
        url=endpoint.url,
        adapter_config=endpoint.adapter_config,
        etag=endpoint.etag or None,
        last_modified=endpoint.last_modified or None,
    )
    try:
        with Fetcher() as fetcher:
            result = adapter_for(endpoint.kind).fetch(request, fetcher)
    except FetchError as error:
        run.http_status = error.http_status
        run.error_kind = error.kind.value
        run.error_message = error.message[:512]
        run.will_retry = error.retryable and retry_allowed
        return _finalize(
            run,
            endpoint,
            started,
            IngestionRun.Status.FAILED,
            logger,
            retry_after=error.retry_after,
        )

    if result.not_modified:
        return _finalize(
            run,
            endpoint,
            started,
            IngestionRun.Status.NO_CHANGE,
            logger,
            etag=result.etag,
            last_modified=result.last_modified,
        )

    run.items_received = len(result.items) + len(result.rejected)
    run.items_rejected = len(result.rejected)
    for rejection in result.rejected:
        logger.warning(
            "News item rejected by adapter",
            extra={"position": rejection.position, "rejection_reason": rejection.reason},
        )

    limit = settings.NEWS_INGEST_MAX_ITEMS_PER_RUN
    accepted = result.items[:limit]
    overflow = len(result.items) - len(accepted)
    if overflow:
        run.items_rejected += overflow
        logger.warning(
            "News ingestion item limit applied",
            extra={"limit": limit, "overflow": overflow},
        )

    fetched_at = timezone.now()
    for position, item in enumerate(accepted):
        identity = external_key(item.external_id, item.url)
        if isinstance(identity, MissingIdentity):
            run.items_rejected += 1
            logger.warning(
                "News item rejected during intake",
                extra={"position": position, "rejection_reason": "MISSING_IDENTITY"},
            )
            continue
        stored, truncated = _stored_payload(item)
        digest = payload_hash(stored)
        if truncated:
            logger.warning(
                "News item payload truncated",
                extra={
                    "position": position,
                    "external_key_kind": identity.kind.value,
                    "truncated": True,
                },
            )
        _, outcome = _persist_item(endpoint, run, identity, item, stored, digest, fetched_at)
        if outcome == "created":
            run.raw_created += 1
        elif outcome == "changed":
            run.raw_changed += 1
        else:
            run.raw_unchanged += 1

    pending_ids = list(
        RawArticle.objects.filter(endpoint=endpoint, status=RawArticle.Status.PENDING)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
    )
    processed_pending = False
    for raw_id in pending_ids:
        try:
            # The current run's context, not the row's receipt provenance: a
            # replayed PENDING row belongs to an older ingestion_run.
            outcome = process_raw_article(raw_id, logger=logger)
        except (
            Exception
        ) as error:  # isolate one processing row; never suppress fetch/intake defects
            run.items_failed += 1
            logger.error(
                "News raw processing failed",
                extra={
                    "raw_article_id": raw_id,
                    "exception_class": type(error).__name__,
                },
            )
            continue
        run.items_processed += 1
        _count_processing(run, outcome)
        if outcome.state is ProcessState.PROCESSED:
            processed_pending = True

    if run.items_rejected or run.items_failed or run.raw_rejected:
        status = IngestionRun.Status.PARTIAL
    elif run.raw_created or run.raw_changed or processed_pending:
        status = IngestionRun.Status.SUCCEEDED
    else:
        status = IngestionRun.Status.NO_CHANGE
    return _finalize(
        run,
        endpoint,
        started,
        status,
        logger,
        etag=result.etag,
        last_modified=result.last_modified,
    )
