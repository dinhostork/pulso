"""Atomically normalize one RawArticle and apply the dedup decision (ADR-0010).

The decision itself lives in `news.domain.dedup`. This module only reads
candidates, hands them to that pure function, and turns the returned value
object into persistence state. Under a race, PostgreSQL's uniqueness is the
arbiter: a rejected insert is re-read and re-decided exactly once.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from news.domain.dedup import ArticleMatch, Decision, DecisionKind, decide
from news.domain.normalization import NormalizedArticle, Rejection, normalize
from news.models import Article, RawArticle, Source

LOGGER = logging.getLogger("pulso.news.process")


class ProcessState(StrEnum):
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class ProcessOutcome:
    """Plain result values the #16 ingestion loop counts, never a model."""

    raw_id: int
    state: ProcessState
    outcome: str = ""
    rejection_reason: str = ""
    article_id: int | None = None


@dataclass(frozen=True)
class _Candidates:
    """Candidate Articles as ORM rows, kept beside their pure snapshots."""

    by_external_id: Article | None
    by_canonical_url: Article | None
    by_fingerprint: Article | None

    def of(self, article_id: int) -> Article:
        for candidate in (self.by_external_id, self.by_canonical_url, self.by_fingerprint):
            if candidate is not None and candidate.pk == article_id:
                return candidate
        raise LookupError(f"Decision referenced an Article outside its candidates: {article_id}")


def _snapshot(article: Article | None) -> ArticleMatch | None:
    if article is None:
        return None
    return ArticleMatch(
        id=article.pk,
        source_id=article.source_id,
        canonical_url=article.canonical_url,
        content_fingerprint=article.content_fingerprint,
    )


def _reject(
    raw: RawArticle,
    reason: str,
    *,
    outcome: str = RawArticle.Outcome.NONE,
    article: Article | None = None,
) -> ProcessOutcome:
    raw.status = RawArticle.Status.REJECTED
    raw.outcome = outcome
    raw.rejection_reason = reason
    raw.article = article
    raw.processed_at = timezone.now()
    raw.save(update_fields=["status", "outcome", "rejection_reason", "article", "processed_at"])
    return ProcessOutcome(
        raw_id=raw.pk,
        state=ProcessState.REJECTED,
        outcome=outcome,
        rejection_reason=reason,
        article_id=article.pk if article is not None else None,
    )


def _reject_normalization(raw: RawArticle, reason: str) -> ProcessOutcome:
    result = _reject(raw, reason)
    LOGGER.warning(
        "Raw article normalization rejected",
        extra={"raw_article_id": raw.pk, "external_key": raw.external_key, "reason": reason},
    )
    return result


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
    return ProcessOutcome(
        raw_id=raw.pk,
        state=ProcessState.PROCESSED,
        outcome=outcome,
        article_id=article.pk,
    )


def _candidates(source: Source, normalized: NormalizedArticle) -> _Candidates:
    """Read the three candidates the decision may consult, in precedence order.

    Provider ids are scoped to the Source; a canonical URL is global and carries
    its owner. The fingerprint query is skipped when an identity candidate
    already exists or the material is too short, because the precedence in
    `decide` makes that candidate unobservable in both cases.
    """

    by_external_id = None
    if normalized.external_id:
        by_external_id = (
            Article.objects.filter(source=source, external_id=normalized.external_id)
            .order_by("pk")
            .first()
        )
    by_canonical_url = (
        Article.objects.select_related("source")
        .filter(canonical_url=normalized.canonical_url)
        .order_by("pk")
        .first()
    )
    by_fingerprint = None
    eligible = normalized.fingerprint_input_length >= settings.NEWS_CONTENT_FINGERPRINT_MIN_CHARS
    if by_external_id is None and by_canonical_url is None and eligible:
        by_fingerprint = (
            Article.objects.filter(content_fingerprint=normalized.content_fingerprint)
            .order_by("created_at", "pk")
            .first()
        )
    return _Candidates(by_external_id, by_canonical_url, by_fingerprint)


def _decide(source: Source, normalized: NormalizedArticle) -> tuple[Decision, _Candidates]:
    candidates = _candidates(source, normalized)
    decision = decide(
        normalized,
        source_id=source.pk,
        by_external_id=_snapshot(candidates.by_external_id),
        by_canonical_url=_snapshot(candidates.by_canonical_url),
        by_fingerprint=_snapshot(candidates.by_fingerprint),
        min_fingerprint_chars=settings.NEWS_CONTENT_FINGERPRINT_MIN_CHARS,
    )
    return decision, candidates


def _inserts_article(kind: DecisionKind) -> bool:
    return kind in (DecisionKind.CREATE, DecisionKind.CONTENT_DUPLICATE)


def _insert(
    raw: RawArticle, normalized: NormalizedArticle, source: Source, decision: Decision
) -> ProcessOutcome:
    """Create one Article inside a savepoint so a race leaves the outer atomic usable."""

    content_duplicate = decision.kind is DecisionKind.CONTENT_DUPLICATE
    duplicate_of_id = decision.article.id if content_duplicate else None
    with transaction.atomic():
        article = Article.objects.create(
            source=source,
            duplicate_of_id=duplicate_of_id,
            first_seen_at=raw.fetched_at,
            **_article_values(raw, normalized),
        )
    outcome = (
        RawArticle.Outcome.CONTENT_DUPLICATE
        if content_duplicate
        else RawArticle.Outcome.ARTICLE_CREATED
    )
    return _finish(raw, article, outcome)


def _update(raw: RawArticle, normalized: NormalizedArticle, article: Article) -> ProcessOutcome:
    """Apply the new revision in place; identity and provenance of origin stay put."""

    values = _article_values(raw, normalized)
    for field, value in values.items():
        setattr(article, field, value)
    article.save(update_fields=[*values, "updated_at"])
    return _finish(raw, article, RawArticle.Outcome.ARTICLE_UPDATED)


def _reject_identity_conflict(
    raw: RawArticle, normalized: NormalizedArticle, decision: Decision
) -> ProcessOutcome:
    reason = RawArticle.Outcome.IDENTITY_CONFLICT
    result = _reject(raw, reason, outcome=reason)
    LOGGER.warning(
        "News publication identity conflict",
        extra={
            "raw_article_id": raw.pk,
            "external_id_article_id": decision.article.id,
            "canonical_url_article_id": decision.conflicting.id,
            "canonical_url": normalized.canonical_url,
        },
    )
    return result


def _reject_source_identity_conflict(
    raw: RawArticle, normalized: NormalizedArticle, existing: Article
) -> ProcessOutcome:
    reason = RawArticle.Outcome.SOURCE_IDENTITY_CONFLICT
    # The existing Article is never touched; the link is investigation evidence.
    result = _reject(raw, reason, outcome=reason, article=existing)
    LOGGER.warning(
        "News canonical URL owned by another Source",
        extra={
            "raw_article_id": raw.pk,
            "article_id": existing.pk,
            "incoming_source_slug": raw.endpoint.source.slug,
            "existing_source_slug": existing.source.slug,
            "incoming_endpoint_id": raw.endpoint_id,
            "existing_endpoint_id": existing.endpoint_id,
            "canonical_url": normalized.canonical_url,
        },
    )
    return result


def _apply(
    raw: RawArticle,
    normalized: NormalizedArticle,
    decision: Decision,
    candidates: _Candidates,
) -> ProcessOutcome:
    """Turn a decision that creates no Article into RawArticle state."""

    if decision.kind is DecisionKind.UPDATE:
        return _update(raw, normalized, candidates.of(decision.article.id))
    if decision.kind is DecisionKind.IDENTITY_DUPLICATE:
        return _finish(
            raw, candidates.of(decision.article.id), RawArticle.Outcome.IDENTITY_DUPLICATE
        )
    if decision.kind is DecisionKind.IDENTITY_CONFLICT:
        return _reject_identity_conflict(raw, normalized, decision)
    return _reject_source_identity_conflict(raw, normalized, candidates.of(decision.article.id))


def _persist(raw: RawArticle, normalized: NormalizedArticle) -> ProcessOutcome:
    source = raw.endpoint.source
    decision, candidates = _decide(source, normalized)
    if _inserts_article(decision.kind):
        try:
            return _insert(raw, normalized, source, decision)
        except IntegrityError:
            # The savepoint above is rolled back, so the outer transaction is
            # usable: read the winning row once and re-decide exactly once.
            decision, candidates = _decide(source, normalized)
            if _inserts_article(decision.kind):
                # Not the expected identity race; never swallow or retry blindly.
                raise
    return _apply(raw, normalized, decision, candidates)


def process_raw_article(raw_id: int) -> ProcessOutcome:
    """Lock one row without waiting and process each pending row exactly once."""

    with transaction.atomic():
        raw = (
            RawArticle.objects.select_related("endpoint__source")
            # Lock this revision only: `of=("self",)` keeps the joined Source
            # and endpoint rows unlocked, so revisions of one Source are not
            # serialized against each other and the dedup race stays a database
            # uniqueness race (ADR-0010).
            .select_for_update(skip_locked=True, of=("self",))
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
            return _reject_normalization(raw, normalized.reason)
        return _persist(raw, normalized)
