"""Stable operational log context for News ingestion (issue #20).

One ingestion execution emits records from three modules (intake, processing
and the task adapters). They all carry the same identifying context, so a
single run can be followed in the logs without correlating by timestamp.

The context is a snapshot of primitives, never a live ORM object: formatting a
log record must not query the database or observe a later mutation.
"""

import logging

DEFAULT_LOGGER = "pulso.news.ingest"

#: The stable fields every record of one ingestion execution should carry.
INGESTION_CONTEXT_FIELDS = (
    "source_id",
    "source_slug",
    "endpoint_id",
    "adapter",
    "run_id",
    "task_id",
    "attempt",
    "trigger",
)


class IngestionLoggerAdapter(logging.LoggerAdapter):
    """Merge stable context into every record instead of replacing `extra`.

    `logging.LoggerAdapter` replaces a call site's `extra` with the adapter's
    own by default, which would silently drop per-call fields such as
    `position`. Here both are merged, and the call site wins on a key
    collision so a more specific value is never masked by the run context.
    """

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs

    def bind(self, **context) -> "IngestionLoggerAdapter":
        """Derive a logger with additional or refined context."""

        return IngestionLoggerAdapter(self.logger, {**self.extra, **context})


def ingestion_logger(
    logger: logging.Logger | str = DEFAULT_LOGGER, /, **context
) -> IngestionLoggerAdapter:
    """Build a contextual logger for one ingestion execution.

    Only the context actually known is attached: a task-driven processing run
    has no current IngestionRun, and a poll summary has no endpoint at all.
    """

    target = logging.getLogger(logger) if isinstance(logger, str) else logger
    return IngestionLoggerAdapter(target, dict(context))


def endpoint_context(endpoint) -> dict:
    """Snapshot the Source/endpoint half of the context from a model instance."""

    return {
        "source_id": endpoint.source_id,
        "source_slug": endpoint.source.slug,
        "endpoint_id": endpoint.pk,
        "adapter": endpoint.kind,
    }


def run_context(run) -> dict:
    """Snapshot the run half of the context from a model instance."""

    return {
        "run_id": run.pk,
        "task_id": run.task_id,
        "attempt": run.attempt,
        "trigger": run.trigger,
    }
