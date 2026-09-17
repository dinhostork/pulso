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
from news.application.process import process_raw_article
from news.application.registry import adapter_for
from news.domain.fingerprints import payload_hash
from news.domain.identity import ExternalKey, MissingIdentity, external_key
from news.models import IngestionRun, RawArticle, SourceEndpoint

LOGGER = logging.getLogger("pulso.news.ingest")


@dataclass(frozen=True)
class RunSummary:
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


def _summary(run: IngestionRun) -> RunSummary:
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
    )


def _log_context(run: IngestionRun, endpoint: SourceEndpoint) -> dict:
    return {
        "run_id": run.pk,
        "endpoint_id": endpoint.pk,
        "source_id": endpoint.source_id,
        "adapter_kind": endpoint.kind,
        "trigger": run.trigger,
        "attempt": run.attempt,
        "task_id": run.task_id,
    }


def _finalize(
    run: IngestionRun,
    endpoint: SourceEndpoint,
    started: float,
    status: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
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
    LOGGER.info(
        "News ingestion run finalized",
        extra={
            **_log_context(run, endpoint),
            "status": run.status,
            "items_received": run.items_received,
            "items_rejected": run.items_rejected,
            "raw_created": run.raw_created,
            "raw_changed": run.raw_changed,
            "raw_unchanged": run.raw_unchanged,
            "items_failed": run.items_failed,
            "duration_ms": run.duration_ms,
        },
    )
    return _summary(run)


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


def ingest_endpoint(
    endpoint_id: int, *, trigger: str, attempt: int = 0, task_id: str = ""
) -> RunSummary:
    """Fetch one endpoint, persist raw revisions, and invoke the processing stub."""

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
    LOGGER.info("News ingestion run started", extra=_log_context(run, endpoint))

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
        run.will_retry = error.retryable
        return _finalize(run, endpoint, started, IngestionRun.Status.FAILED)

    if result.not_modified:
        return _finalize(
            run,
            endpoint,
            started,
            IngestionRun.Status.NO_CHANGE,
            etag=result.etag,
            last_modified=result.last_modified,
        )

    run.items_received = len(result.items) + len(result.rejected)
    run.items_rejected = len(result.rejected)
    for rejection in result.rejected:
        LOGGER.warning(
            "News item rejected by adapter",
            extra={
                **_log_context(run, endpoint),
                "position": rejection.position,
                "reason": rejection.reason,
            },
        )

    limit = settings.NEWS_INGEST_MAX_ITEMS_PER_RUN
    accepted = result.items[:limit]
    overflow = len(result.items) - len(accepted)
    if overflow:
        run.items_rejected += overflow
        LOGGER.warning(
            "News ingestion item limit applied",
            extra={**_log_context(run, endpoint), "item_limit": limit, "overflow": overflow},
        )

    fetched_at = timezone.now()
    for position, item in enumerate(accepted):
        identity = external_key(item.external_id, item.url)
        if isinstance(identity, MissingIdentity):
            run.items_rejected += 1
            LOGGER.warning(
                "News item rejected during intake",
                extra={
                    **_log_context(run, endpoint),
                    "position": position,
                    "reason": "MISSING_IDENTITY",
                },
            )
            continue
        stored, truncated = _stored_payload(item)
        digest = payload_hash(stored)
        if truncated:
            LOGGER.warning(
                "News item payload truncated",
                extra={
                    **_log_context(run, endpoint),
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
    for raw_id in pending_ids:
        try:
            process_raw_article(raw_id)
            run.items_processed += 1
        except (
            Exception
        ) as error:  # isolate one processing row; never suppress fetch/intake defects
            run.items_failed += 1
            LOGGER.error(
                "News raw processing failed",
                extra={
                    **_log_context(run, endpoint),
                    "raw_id": raw_id,
                    "exception_class": type(error).__name__,
                },
            )

    if run.items_rejected or run.items_failed:
        status = IngestionRun.Status.PARTIAL
    elif run.raw_created or run.raw_changed:
        status = IngestionRun.Status.SUCCEEDED
    else:
        status = IngestionRun.Status.NO_CHANGE
    return _finalize(
        run,
        endpoint,
        started,
        status,
        etag=result.etag,
        last_modified=result.last_modified,
    )
