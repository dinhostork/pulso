"""Stable operational log context for News ingestion (#20) and Story processing (#33).

One ingestion execution emits records from three modules (intake, processing
and the task adapters). They all carry the same identifying context, so a
single run can be followed in the logs without correlating by timestamp.
Story processing uses the same adapter with its own context: every step of one
Article's Story processing carries its `article_id`, and every step of one
Story refresh its `story_id`.

The context is a snapshot of primitives, never a live ORM object: formatting a
log record must not query the database or observe a later mutation.
"""

import logging
import time

DEFAULT_LOGGER = "pulso.news.ingest"
STORY_LOGGER = "pulso.news.stories"

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

#: The stable fields of Story processing. Only those known at a step are bound:
#: a Story refresh has no Article, and a synchronous operator call no task.
STORY_CONTEXT_FIELDS = (
    "article_id",
    "story_id",
    "model_key",
    "task_id",
    "attempt",
    "trigger",
)


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Merge stable context into every record instead of replacing `extra`.

    `logging.LoggerAdapter` replaces a call site's `extra` with the adapter's
    own by default, which would silently drop per-call fields such as
    `position`. Here both are merged, and the call site wins on a key
    collision so a more specific value is never masked by the run context.
    """

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs

    def bind(self, **context) -> "ContextLoggerAdapter":
        """Derive a logger with additional or refined context."""

        return type(self)(self.logger, {**self.extra, **context})


# The ingestion name predates the Story context; both share one adapter.
IngestionLoggerAdapter = ContextLoggerAdapter


def _adapter(logger: logging.Logger | str, context: dict) -> ContextLoggerAdapter:
    target = logging.getLogger(logger) if isinstance(logger, str) else logger
    return ContextLoggerAdapter(target, context)


def ingestion_logger(
    logger: logging.Logger | str = DEFAULT_LOGGER, /, **context
) -> ContextLoggerAdapter:
    """Build a contextual logger for one ingestion execution.

    Only the context actually known is attached: a task-driven processing run
    has no current IngestionRun, and a poll summary has no endpoint at all.
    """

    return _adapter(logger, dict(context))


def story_logger(logger: logging.Logger | str = STORY_LOGGER, /, **context) -> ContextLoggerAdapter:
    """Build a contextual logger for Story processing (`STORY_CONTEXT_FIELDS`)."""

    return _adapter(logger, dict(context))


def task_context(request, *, trigger: str) -> dict:
    """The task half of the Story context: a retry is always `RETRY`."""

    attempt = request.retries
    return {
        "task_id": request.id or "",
        "attempt": attempt,
        "trigger": "RETRY" if attempt else trigger,
    }


def elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.monotonic()` reading, never negative."""

    return max(0, int((time.monotonic() - started) * 1000))


#: The one message pair of Story step records; `step` names the step.
STEP_COMPLETED = "News Story step completed"
STEP_FAILED = "News Story step failed"


def log_step(
    logger: ContextLoggerAdapter, step: str, started: float, *, failed: bool = False, **fields
) -> None:
    """Emit one record for one Story pipeline step, with its monotonic duration."""

    extra = {"step": step, "duration_ms": elapsed_ms(started), **fields}
    # stacklevel=2 attributes the record to the step, not to this helper.
    if failed:
        logger.warning(STEP_FAILED, extra=extra, stacklevel=2)
    else:
        logger.info(STEP_COMPLETED, extra=extra, stacklevel=2)


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
