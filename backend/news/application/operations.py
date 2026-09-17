"""Operational maintenance of ingestion history (issue #20).

Retention lives here, not in a Celery task or a management command, so the
weekly schedule and the operator command share one rule.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from news.logging import ingestion_logger
from news.models import IngestionRun

OPERATIONS_LOGGER = "pulso.news.operations"


def stale_running_cutoff(now: datetime | None = None) -> datetime:
    """The instant before which a RUNNING run is considered stale.

    One definition shared by the poll dispatcher's in-flight guard (#19) and
    `news_runs --stale` (#20), so the two views can never disagree.
    """

    return (now or timezone.now()) - timedelta(seconds=settings.NEWS_STALE_RUNNING_SECONDS)


def stale_running_runs(now: datetime | None = None):
    """RUNNING runs that started too long ago to still be in flight."""

    return IngestionRun.objects.filter(
        status=IngestionRun.Status.RUNNING, started_at__lt=stale_running_cutoff(now)
    )


def prune_ingestion_runs(*, days: int | None = None) -> int:
    """Delete finalized runs that finished before the retention cutoff.

    Retention is measured from `finished_at`, so a long execution is never
    expired while it is still running. A RUNNING run is never deleted whatever
    its age: only `news_runs --stale` reports those, and an operator decides.

    RawArticle and Article rows are never deleted. `RawArticle.ingestion_run`
    is nulled by the existing `on_delete=SET_NULL` rule, which drops the link
    to discarded operational history without touching receipt provenance.
    """

    retention_days = settings.NEWS_RUN_RETENTION_DAYS if days is None else days
    if retention_days < 1:
        raise ValueError("Retention must be at least one day")
    cutoff = timezone.now() - timedelta(days=retention_days)
    _, per_model = (
        IngestionRun.objects.filter(finished_at__isnull=False, finished_at__lt=cutoff)
        .exclude(status=IngestionRun.Status.RUNNING)
        .delete()
    )
    # Report runs only. Nothing cascades from a run today, and counting a
    # future related deletion as a pruned run would be misleading.
    deleted = per_model.get(IngestionRun._meta.label, 0)
    logger: logging.LoggerAdapter = ingestion_logger(OPERATIONS_LOGGER)
    logger.info(
        "News ingestion run history pruned",
        extra={
            "deleted": deleted,
            "retention_days": retention_days,
            "cutoff": cutoff.isoformat(),
        },
    )
    return deleted
