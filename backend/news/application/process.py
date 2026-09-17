"""Atomically normalize one RawArticle and persist its publication identity."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from django.db import transaction
from django.utils import timezone

from news.domain.normalization import NormalizedArticle, Rejection, normalize
from news.models import Article, RawArticle

LOGGER = logging.getLogger("pulso.news.process")


class ProcessState(StrEnum):
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class ProcessOutcome:
    raw_id: int
    state: ProcessState


def _reject(raw: RawArticle, reason: str) -> ProcessOutcome:
    raw.status = RawArticle.Status.REJECTED
    raw.outcome = RawArticle.Outcome.NONE
    raw.rejection_reason = reason
    raw.processed_at = timezone.now()
    raw.save(update_fields=["status", "outcome", "rejection_reason", "processed_at"])
    LOGGER.warning(
        "Raw article normalization rejected",
        extra={"raw_article_id": raw.pk, "external_key": raw.external_key, "reason": reason},
    )
    return ProcessOutcome(raw.pk, ProcessState.REJECTED)


def _article_values(raw: RawArticle, normalized: NormalizedArticle) -> dict:
    return {
        "endpoint": raw.endpoint,
        "raw_article": raw,
        "external_id": normalized.external_id,
        "canonical_url": normalized.canonical_url,
        "title": normalized.title,
        "description": normalized.description,
        "body_text": normalized.body_text,
        "byline": normalized.byline,
        "published_at": normalized.published_at,
        "language": normalized.language,
        "content_fingerprint": normalized.content_fingerprint,
    }


def _finish(raw: RawArticle, article: Article, outcome: str) -> ProcessOutcome:
    raw.article = article
    raw.status = RawArticle.Status.PROCESSED
    raw.outcome = outcome
    raw.rejection_reason = ""
    raw.processed_at = timezone.now()
    raw.save(update_fields=["article", "status", "outcome", "rejection_reason", "processed_at"])
    return ProcessOutcome(raw.pk, ProcessState.PROCESSED)


def _persist(raw: RawArticle, normalized: NormalizedArticle) -> ProcessOutcome:
    source = raw.endpoint.source
    by_external_id = None
    if normalized.external_id:
        by_external_id = Article.objects.filter(
            source=source, external_id=normalized.external_id
        ).first()
    by_url = Article.objects.filter(canonical_url=normalized.canonical_url).first()
    if by_url is not None and by_url.source_id != source.pk:
        return _reject(raw, "SOURCE_IDENTITY_CONFLICT")
    if by_external_id is not None and by_url is not None and by_external_id.pk != by_url.pk:
        return _reject(raw, "IDENTITY_CONFLICT")

    article = by_external_id or by_url
    if article is None:
        article = Article.objects.create(
            source=source, first_seen_at=raw.fetched_at, **_article_values(raw, normalized)
        )
        return _finish(raw, article, RawArticle.Outcome.ARTICLE_CREATED)
    if article.content_fingerprint == normalized.content_fingerprint:
        return _finish(raw, article, RawArticle.Outcome.IDENTITY_DUPLICATE)

    values = _article_values(raw, normalized)
    for field, value in values.items():
        setattr(article, field, value)
    article.save(update_fields=[*values, "updated_at"])
    return _finish(raw, article, RawArticle.Outcome.ARTICLE_UPDATED)


def process_raw_article(raw_id: int) -> ProcessOutcome:
    """Lock one row without waiting and process each pending row exactly once."""

    with transaction.atomic():
        raw = (
            RawArticle.objects.select_related("endpoint__source")
            .select_for_update(skip_locked=True)
            .filter(pk=raw_id)
            .first()
        )
        if raw is None:
            return ProcessOutcome(raw_id=raw_id, state=ProcessState.LOCKED)
        if raw.status != RawArticle.Status.PENDING:
            return ProcessOutcome(raw_id=raw_id, state=ProcessState.SKIPPED)
        normalized = normalize(
            raw.payload, source_default_language=raw.endpoint.source.default_language
        )
        if isinstance(normalized, Rejection):
            return _reject(raw, normalized.reason)
        return _persist(raw, normalized)
