"""Bounded, idempotent FeedImpression acceptance and retention (ADR-0011, ADR-0012).

An impression is a client-reported qualified exposure of a Story card on
HOME_FEED. Validation proves only the report's shape and bounds: it does not
prove the Story was served or seen, and forged reports remain possible.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from news.models import Story
from reading.models import MAX_IMPRESSION_POSITION, FeedImpression

# Validated v1 policy; see docs/architecture/mobile-feed.md "FeedImpression
# server policy" for the evidence and the #51 client queue it is sized against.
MAX_BODY_BYTES = 32 * 1024
MAX_BATCH_EVENTS = 20
MAX_EVENT_AGE = timedelta(hours=24)
MAX_CLOCK_AHEAD = timedelta(minutes=5)
POLICY_VERSION = 1
MAX_PRUNE_BATCH = 10_000

EVENT_FIELDS = frozenset(
    {
        "event_id",
        "story_id",
        "feed_session_id",
        "position",
        "surface",
        "policy_version",
        "occurred_at",
    }
)
EVENT_UNIQUE = "reading_impression_user_event_unique"
EXPOSURE_UNIQUE = "reading_impression_exposure_unique"
_MAX_STORY_ID = 2**63 - 1
_DECIMAL_ID = re.compile(r"[1-9][0-9]{0,18}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_MAX_TIMESTAMP_CHARS = 40
logger = logging.getLogger("pulso.reading.impressions")


def _now() -> datetime:
    """The server clock; tests replace this function instead of sleeping."""

    return timezone.now()


class ImpressionBatchInvalid(ValueError):
    """The whole batch failed structural validation; nothing was written."""

    def __init__(self, fields: dict[str, list[str]]):
        super().__init__("invalid FeedImpression batch")
        self.fields = fields


@dataclass(frozen=True)
class ImpressionEvent:
    wire_event_id: str
    event_id: uuid.UUID
    story_id: int
    feed_session_id: uuid.UUID
    position: int
    surface: str
    policy_version: int
    occurred_at: datetime


@dataclass(frozen=True)
class ImpressionOutcome:
    event_id: str
    outcome: str
    code: str | None


@dataclass(frozen=True)
class PruneResult:
    cutoff: datetime
    eligible: int
    deleted: int
    batches: int
    applied: bool


def _uuid(value) -> uuid.UUID | None:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        return None
    return uuid.UUID(value)


def _story_id(value) -> int | None:
    if not isinstance(value, str) or not _DECIMAL_ID.fullmatch(value):
        return None
    parsed = int(value)
    return parsed if parsed <= _MAX_STORY_ID else None


def _strict_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event(index: int, value, *, now: datetime, errors: dict) -> ImpressionEvent | None:
    prefix = f"events[{index}]"
    if not isinstance(value, dict):
        errors[prefix] = ["Expected an object."]
        return None
    for name in sorted(set(value) - EVENT_FIELDS, key=str):
        errors[f"{prefix}.{name}"] = ["Unknown field."]
    for name in sorted(EVENT_FIELDS - set(value)):
        errors[f"{prefix}.{name}"] = ["This field is required."]
    if set(value) != EVENT_FIELDS:
        return None

    event_id = _uuid(value["event_id"])
    if event_id is None:
        errors[f"{prefix}.event_id"] = ["Must be a UUID string."]
    story_id = _story_id(value["story_id"])
    if story_id is None:
        errors[f"{prefix}.story_id"] = ["Must be a positive decimal ID string."]
    session_id = _uuid(value["feed_session_id"])
    if session_id is None:
        errors[f"{prefix}.feed_session_id"] = ["Must be a UUID string."]
    position = _strict_int(value["position"])
    if position is None or not 0 <= position <= MAX_IMPRESSION_POSITION:
        errors[f"{prefix}.position"] = [
            f"Must be an integer between 0 and {MAX_IMPRESSION_POSITION}."
        ]
    if value["surface"] != FeedImpression.Surface.HOME_FEED:
        errors[f"{prefix}.surface"] = ["Must be HOME_FEED."]
    if _strict_int(value["policy_version"]) != POLICY_VERSION:
        errors[f"{prefix}.policy_version"] = [f"Must be {POLICY_VERSION}."]
    occurred_at = None
    raw_time = value["occurred_at"]
    if isinstance(raw_time, str) and len(raw_time) <= _MAX_TIMESTAMP_CHARS:
        try:
            occurred_at = parse_datetime(raw_time)
        except ValueError:
            occurred_at = None
    if occurred_at is None or occurred_at.tzinfo is None:
        errors[f"{prefix}.occurred_at"] = ["Must be an ISO-8601 timestamp with a time zone."]
        occurred_at = None
    elif not now - MAX_EVENT_AGE <= occurred_at <= now + MAX_CLOCK_AHEAD:
        errors[f"{prefix}.occurred_at"] = [
            "Must be at most 24 hours old and at most 5 minutes ahead of the server."
        ]
        occurred_at = None

    if any(key.startswith(f"{prefix}.") for key in errors):
        return None
    return ImpressionEvent(
        wire_event_id=value["event_id"],
        event_id=event_id,
        story_id=story_id,
        feed_session_id=session_id,
        position=position,
        surface=value["surface"],
        policy_version=POLICY_VERSION,
        occurred_at=occurred_at,
    )


def validate_batch(data, *, now: datetime) -> tuple[ImpressionEvent, ...]:
    """Validate the whole request body before any write; all or nothing."""

    if not isinstance(data, dict):
        raise ImpressionBatchInvalid({"body": ["Expected an object."]})
    unknown = sorted(set(data) - {"events"}, key=str)
    if unknown:
        raise ImpressionBatchInvalid({str(name): ["Unknown field."] for name in unknown})
    events = data.get("events")
    if not isinstance(events, list):
        raise ImpressionBatchInvalid({"events": ["Expected a list."]})
    if not 1 <= len(events) <= MAX_BATCH_EVENTS:
        raise ImpressionBatchInvalid(
            {"events": [f"Must contain between 1 and {MAX_BATCH_EVENTS} events."]}
        )
    errors: dict[str, list[str]] = {}
    parsed = [_event(index, value, now=now, errors=errors) for index, value in enumerate(events)]
    if errors:
        raise ImpressionBatchInvalid(errors)
    return tuple(parsed)


def _same_report(row: FeedImpression, event: ImpressionEvent) -> bool:
    return (
        row.original_story_id == event.story_id
        and row.feed_session_id == event.feed_session_id
        and row.position == event.position
        and row.surface == event.surface
        and row.policy_version == event.policy_version
        and row.occurred_at == event.occurred_at
    )


def _replayed(row: FeedImpression, event: ImpressionEvent) -> ImpressionOutcome:
    if _same_report(row, event):
        return ImpressionOutcome(event.wire_event_id, "duplicate", None)
    return ImpressionOutcome(event.wire_event_id, "rejected", "event_conflict")


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.__cause__, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


def _accept_one(
    *,
    user,
    event: ImpressionEvent,
    seen: dict[uuid.UUID, FeedImpression],
    known_story_ids: set[int],
    received_at: datetime,
) -> ImpressionOutcome:
    existing = seen.get(event.event_id)
    if existing is not None:
        return _replayed(existing, event)
    if event.story_id not in known_story_ids:
        return ImpressionOutcome(event.wire_event_id, "rejected", "story_not_found")
    try:
        # The savepoint keeps one expected unique violation from aborting the
        # enclosing batch transaction.
        with transaction.atomic():
            row = FeedImpression.objects.create(
                user=user,
                story_id=event.story_id,
                original_story_id=event.story_id,
                event_id=event.event_id,
                feed_session_id=event.feed_session_id,
                position=event.position,
                surface=event.surface,
                policy_version=event.policy_version,
                occurred_at=event.occurred_at,
                received_at=received_at,
            )
    except IntegrityError as error:
        if _violated_constraint(error) not in {EVENT_UNIQUE, EXPOSURE_UNIQUE}:
            raise
        # A parallel delivery committed first. The first persisted report wins.
        winner = FeedImpression.objects.filter(user=user, event_id=event.event_id).first()
        if winner is None:
            return ImpressionOutcome(event.wire_event_id, "rejected", "event_conflict")
        seen[event.event_id] = winner
        return _replayed(winner, event)
    seen[event.event_id] = row
    return ImpressionOutcome(event.wire_event_id, "accepted", None)


def _log_batch(outcome: str, started: float, **counts) -> None:
    logger.info(
        "Feed impression batch completed",
        extra={
            "operation": "feed_impression_batch",
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000),
            **counts,
        },
    )


def log_rejected_request(reason: str, started: float) -> None:
    """Record a transport-level rejection (size, media type, JSON) by reason only."""

    _log_batch("invalid", started, rejection_reason=reason, count=0)


def accept_feed_impressions(*, user, data, started: float) -> tuple[ImpressionOutcome, ...]:
    """Validate a parsed request body, then persist each new exposure once.

    `user` and `received_at` are server-derived. Outcomes follow input order and
    never mutate an already accepted row.
    """

    now = _now()
    try:
        events = validate_batch(data, now=now)
    except ImpressionBatchInvalid:
        _log_batch("invalid", started, rejection_reason="structure", count=0)
        raise

    with transaction.atomic():
        seen = {
            row.event_id: row
            for row in FeedImpression.objects.filter(
                user=user, event_id__in={event.event_id for event in events}
            )
        }
        known_story_ids = set(
            Story.objects.filter(pk__in={event.story_id for event in events}).values_list(
                "pk", flat=True
            )
        )
        outcomes = tuple(
            _accept_one(
                user=user,
                event=event,
                seen=seen,
                known_story_ids=known_story_ids,
                received_at=now,
            )
            for event in events
        )

    _log_batch(
        "processed",
        started,
        count=len(outcomes),
        accepted_count=sum(item.outcome == "accepted" for item in outcomes),
        duplicate_count=sum(item.outcome == "duplicate" for item in outcomes),
        conflict_count=sum(item.code == "event_conflict" for item in outcomes),
        rejected_count=sum(item.code == "story_not_found" for item in outcomes),
    )
    return outcomes


def retention_cutoff(*, days: int | None = None) -> datetime:
    """The receipt time before which rows are past retention."""

    retention_days = settings.READING_IMPRESSION_RETENTION_DAYS if days is None else days
    if retention_days < 1:
        raise ValueError("Retention must be at least one day")
    return _now() - timedelta(days=retention_days)


def prune_feed_impressions(
    *, cutoff: datetime, batch_size: int, max_batches: int | None = None, apply: bool = False
) -> PruneResult:
    """Count, and with `apply` delete, rows received before one fixed cutoff.

    Each batch deletes at most `batch_size` rows in its own short transaction,
    oldest first, so re-running with the same cutoff resumes where it stopped.
    """

    if not 1 <= batch_size <= MAX_PRUNE_BATCH:
        raise ValueError(f"batch_size must be between 1 and {MAX_PRUNE_BATCH}")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be positive")
    started = time.monotonic()
    eligible_rows = FeedImpression.objects.filter(received_at__lt=cutoff)
    eligible = eligible_rows.count()
    deleted = 0
    batches = 0
    while apply and (max_batches is None or batches < max_batches):
        ids = list(
            eligible_rows.order_by("received_at", "pk").values_list("pk", flat=True)[:batch_size]
        )
        if not ids:
            break
        with transaction.atomic():
            count, _ = FeedImpression.objects.filter(pk__in=ids, received_at__lt=cutoff).delete()
        deleted += count
        batches += 1
        if len(ids) < batch_size:
            break
    logger.info(
        "Feed impression retention completed",
        extra={
            "operation": "feed_impression_prune",
            "outcome": "applied" if apply else "dry_run",
            "eligible_count": eligible,
            "deleted": deleted,
            "batch_count": batches,
            "cutoff": cutoff.isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )
    return PruneResult(cutoff, eligible, deleted, batches, apply)
