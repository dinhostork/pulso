"""RawArticle processing entry point; normalization is implemented in issue #17."""

from dataclasses import dataclass
from enum import StrEnum

from django.db import transaction

from news.models import RawArticle


class ProcessState(StrEnum):
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class ProcessOutcome:
    raw_id: int
    state: ProcessState


def process_raw_article(raw_id: int) -> ProcessOutcome:
    """Lock one row without waiting; the issue #16 stub deliberately leaves it PENDING."""

    with transaction.atomic():
        raw = RawArticle.objects.select_for_update(skip_locked=True).filter(pk=raw_id).first()
        if raw is None:
            return ProcessOutcome(raw_id=raw_id, state=ProcessState.LOCKED)
        if raw.status != RawArticle.Status.PENDING:
            return ProcessOutcome(raw_id=raw_id, state=ProcessState.SKIPPED)
        return ProcessOutcome(raw_id=raw_id, state=ProcessState.PENDING)
