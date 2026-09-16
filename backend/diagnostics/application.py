"""Diagnostic application function invoked by the Celery task adapter.

Kept separate from diagnostics/tasks.py so the task stays a thin adapter,
per the task-boundary convention in ADR-0005.
"""

import logging
from datetime import UTC, datetime

logger = logging.getLogger("pulso.diagnostics")


def execute_diagnostic_ping() -> dict:
    """Prove that a worker received and executed a task through Redis.

    Reads and writes no domain state, so repeated or duplicate delivery
    (ADR-0005: tasks may execute more than once) cannot mutate authoritative
    data; the function is idempotent by having no persistent side effect.
    """
    executed_at = datetime.now(tz=UTC).isoformat()
    logger.info("diagnostic ping executed at %s", executed_at)
    return {"ok": True, "executed_at": executed_at}
