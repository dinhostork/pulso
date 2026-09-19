"""JSON-lines formatting for the `pulso` logger tree (issue #20).

Standard library only: no logging dependency is introduced, and a future log
shipper needs no change because every record is one JSON object on one line.

The formatter never serializes `record.__dict__` wholesale. Only the fields in
`SAFE_FIELDS` below are emitted, so an accidental
`extra={"payload": ...}` at some future call site cannot leak publication
content, headers, credentials or adapter configuration into the logs. Adding a
field to that tuple is a deliberate decision; logs are operational metadata,
never a copy of article content.
"""

import json
import logging
from datetime import UTC, datetime

#: Operational identifiers, statuses and counts that may be emitted. Content
#: fields (title, description, body_text, summary_html, content_html, payload,
#: adapter_config, headers, credentials) and Story-derived content (synthesis
#: text, prompts, provider responses, vectors) are deliberately absent and must
#: never be added.
SAFE_FIELDS = (
    # Stable ingestion context (news/logging.py).
    "source_id",
    "source_slug",
    "endpoint_id",
    "adapter",
    "run_id",
    "task_id",
    "attempt",
    "trigger",
    # Per-item provenance and result.
    "raw_article_id",
    "position",
    "external_key",
    "external_key_kind",
    "truncated",
    "state",
    "outcome",
    "rejection_reason",
    "article_id",
    # Run result.
    "status",
    "error_kind",
    "http_status",
    "duration_ms",
    "will_retry",
    "retry_after",
    # Run counters, mirroring IngestionRun.
    "items_received",
    "items_rejected",
    "raw_created",
    "raw_changed",
    "raw_unchanged",
    "items_processed",
    "items_failed",
    "identity_duplicates",
    "content_duplicates",
    "raw_rejected",
    "source_identity_conflicts",
    # Task orchestration.
    "countdown",
    "dispatched",
    "skipped_not_due",
    "skipped_running",
    "skipped_inactive",
    "limit",
    "overflow",
    "exception_class",
    # Identity conflicts (identifiers only).
    "canonical_url",
    "external_id_article_id",
    "canonical_url_article_id",
    "incoming_source_slug",
    "existing_source_slug",
    "incoming_endpoint_id",
    "existing_endpoint_id",
    # Story processing (#29): identifiers, versions and states only.
    "story_id",
    "pipeline_key",
    "attempts",
    # Story enrichment (#30): counts and extractor identity only.
    "topic_count",
    "entity_count",
    "model_key",
    # Story synthesis (#31): counts only.
    "input_article_count",
    "element_count",
    # Story observability (#33): step names, decisions, numbers and states.
    # `story_id`, `model_key`, `topic_count`, `entity_count`, `error_kind`,
    # `duration_ms`, `outcome` and `state` are already listed above.
    "step",
    "failed_step",
    "candidate_count",
    "chosen_story_id",
    "distance",
    "threshold",
    "decision",
    "match_reason",
    "match_rule",
    "matcher_key",
    "member_count",
    "article_count",
    "source_count",
    "synthesis_source_count",
    "refresh_state",
    "refresh_reason",
    # Retention.
    "deleted",
    "retention_days",
    "cutoff",
)

#: Bound for any single string value, so one record can never grow unbounded.
MAX_VALUE_CHARS = 512

_MISSING = object()


def _jsonable(value: object) -> object:
    """Keep primitives; describe anything else by type instead of by content."""

    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        # str() normalizes str subclasses such as Django's TextChoices members.
        return str(value)[:MAX_VALUE_CHARS]
    return type(value).__name__


class JsonLinesFormatter(logging.Formatter):
    """Render one log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # getMessage() keeps existing %-parameterized messages working.
            "message": record.getMessage()[:MAX_VALUE_CHARS],
        }
        for field in SAFE_FIELDS:
            value = getattr(record, field, _MISSING)
            if value is not _MISSING:
                payload[field] = _jsonable(value)
        if record.exc_info and record.exc_info[0] is not None:
            # The class only: exception arguments and tracebacks may carry
            # transport data or secrets, and Celery/Django keep their own
            # traceback output on their own loggers.
            payload.setdefault("exception_class", record.exc_info[0].__name__)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
