"""Celery task adapters. Tasks delegate to application services (ADR-0005)."""

from celery import shared_task

from diagnostics.application import execute_diagnostic_ping


@shared_task
def diagnostic_ping() -> dict:
    """Harmless diagnostic task proving broker/worker connectivity.

    Not a product task: it exists to validate the Celery/Redis infrastructure
    (issue #4) and is safe to run repeatedly, concurrently or on a schedule.
    """
    return execute_diagnostic_ping()
