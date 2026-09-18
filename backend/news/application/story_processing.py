"""Per-Article Story processing: embed, match, reconcile and reprocess (#29).

This module owns the state transitions of `ArticleStoryProcessing` and the
freshness rule. The work itself stays where it lives: embedding in
`news.application.embeddings` (#25), retrieval and matching in
`news.application.story_matching` (#27/#28). The Celery tasks in
`news.tasks` only resolve identifiers, call these functions and schedule the
retries they report.

Pipeline and freshness
    `pipeline_key` is `<embedding_model_key>|<matcher_key>` — the embedding
    model and the matching policy an Article was processed with. A row is
    fresh only when it is MATCHED, both halves equal the configured pair and
    the Article still has its primary association. Either half moving alone
    makes it stale.

States
    PENDING   recorded for the current pair, not yet embedded
    EMBEDDED  ArticleEmbedding stored for `embedding_model_key`
    MATCHED   primary StoryArticle assigned under both keys
    FAILED    the last execution failed; `error_kind`/`error_message` say how

Failure classification (the contract the tasks rely on)
    transient, retried with backoff:
        DATABASE_UNAVAILABLE      django.db.OperationalError
        PROVIDER_UNAVAILABLE      provider timeout/connection (EmbeddingError)
        STORY_NO_LONGER_ACTIVE    chosen Story archived twice during matching
    permanent, recorded and not retried by the task:
        every other EmbeddingError kind (missing model or optional group,
        invalid input, invalid output, dimension mismatch)
        MISSING_EMBEDDING         matching found no ArticleEmbedding
    anything else is recorded as UNEXPECTED (exception class only) and raised.

    Every failed execution increments `attempts` for the recorded pair.
    Reconciliation stops re-dispatching a FAILED row once `attempts` reaches
    NEWS_STORY_PROCESSING_MAX_ATTEMPTS; it stays visible as FAILED until the
    pipeline pair changes or an operator reprocesses it.

Separation from News Core
    Nothing here writes to `Article`, `RawArticle`, `IngestionRun`, `Source`
    or `SourceEndpoint`. Reprocessing deletes and rebuilds only derived rows:
    the Article's embeddings, its primary StoryArticle and this record.
    Removing an association can leave a Story without members; such a Story
    is never a retrieval candidate (#27), and marking it for refresh and
    archiving it belong to the Story refresh lifecycle (#32).
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from news.application.embeddings import configured_provider, embed_article
from news.application.story_candidates import MissingArticleEmbedding
from news.application.story_matching import MATCHER_KEY, StoryNoLongerActive, match_article
from news.application.story_ports import EmbeddingError
from news.models import Article, ArticleEmbedding, ArticleStoryProcessing, StoryArticle

State = ArticleStoryProcessing.State
ERROR_MESSAGE_MAX_CHARS = 512


@dataclass(frozen=True)
class PipelineKeys:
    embedding_model_key: str
    matcher_key: str

    @property
    def pipeline_key(self) -> str:
        return f"{self.embedding_model_key}|{self.matcher_key}"


def current_keys() -> PipelineKeys:
    """The configured pair; reading the provider identity loads no model."""

    return PipelineKeys(configured_provider().identity.model_key, MATCHER_KEY)


@dataclass(frozen=True)
class StepResult:
    """Plain, log-safe result values; never a model instance or any text."""

    article_id: int
    state: str
    pipeline_key: str
    attempts: int = 0
    error_kind: str = ""
    retryable: bool = False
    story_id: int | None = None


@dataclass(frozen=True)
class _Failure:
    kind: str
    message: str
    retryable: bool


def _classify(error: Exception) -> _Failure | None:
    if isinstance(error, EmbeddingError):
        return _Failure(str(error.kind), error.message, error.retryable)
    if isinstance(error, OperationalError):
        return _Failure("DATABASE_UNAVAILABLE", "Database operation failed.", True)
    if isinstance(error, StoryNoLongerActive):
        return _Failure("STORY_NO_LONGER_ACTIVE", "Chosen Story was archived.", True)
    if isinstance(error, MissingArticleEmbedding):
        return _Failure("MISSING_EMBEDDING", "No embedding for the configured model.", False)
    return None


def _row(article_id: int, keys: PipelineKeys) -> ArticleStoryProcessing:
    row, _ = ArticleStoryProcessing.objects.get_or_create(
        article_id=article_id,
        defaults={
            "embedding_model_key": keys.embedding_model_key,
            "matcher_key": keys.matcher_key,
        },
    )
    return row


def _keys_of(row: ArticleStoryProcessing) -> PipelineKeys:
    return PipelineKeys(row.embedding_model_key, row.matcher_key)


def _result(row: ArticleStoryProcessing, **extra) -> StepResult:
    return StepResult(
        article_id=row.article_id,
        state=row.state,
        pipeline_key=_keys_of(row).pipeline_key,
        attempts=row.attempts,
        error_kind=row.error_kind,
        **extra,
    )


def _primary(article_id: int) -> StoryArticle | None:
    return StoryArticle.objects.filter(article_id=article_id, is_primary=True).first()


def _is_fresh(row: ArticleStoryProcessing, keys: PipelineKeys) -> bool:
    return (
        row.state == State.MATCHED
        and _keys_of(row) == keys
        and StoryArticle.objects.filter(article_id=row.article_id, is_primary=True).exists()
    )


def _record_failure(article_id: int, keys: PipelineKeys, failure: _Failure) -> StepResult:
    with transaction.atomic():
        row = _row(article_id, keys)
        row = ArticleStoryProcessing.objects.select_for_update().get(pk=row.pk)
        same_pipeline = _keys_of(row) == keys
        row.attempts = row.attempts + 1 if same_pipeline else 1
        row.state = State.FAILED
        row.error_kind = failure.kind[:32]
        row.error_message = " ".join(failure.message.split())[:ERROR_MESSAGE_MAX_CHARS]
        row.embedding_model_key = keys.embedding_model_key
        row.matcher_key = keys.matcher_key
        row.save()
    return _result(row, retryable=failure.retryable)


def _guarded(article_id: int, keys: PipelineKeys, step) -> StepResult:
    try:
        return step()
    except Exception as error:
        failure = _classify(error)
        if failure is None:
            _record_failure(article_id, keys, _Failure("UNEXPECTED", type(error).__name__, False))
            raise
        return _record_failure(article_id, keys, failure)


def embed_step(article_id: int) -> StepResult:
    """Store the Article's embedding for the configured model; idempotent."""

    keys = current_keys()
    Article.objects.only("pk").get(pk=article_id)

    def run() -> StepResult:
        row = _row(article_id, keys)
        if _is_fresh(row, keys):
            return _result(row)
        embed_article(article_id, configured_provider())
        with transaction.atomic():
            row = ArticleStoryProcessing.objects.select_for_update().get(pk=row.pk)
            if not _is_fresh(row, keys):
                if _keys_of(row) != keys:
                    row.attempts = 0
                row.state = State.EMBEDDED
                row.embedding_model_key = keys.embedding_model_key
                row.matcher_key = keys.matcher_key
                row.error_kind = ""
                row.error_message = ""
                row.save()
        return _result(row)

    return _guarded(article_id, keys, run)


def match_step(article_id: int) -> StepResult:
    """Assign the Article's primary Story under the configured pair; idempotent.

    An existing primary association made with another matcher policy or
    embedding model is stale and is replaced in the same transaction. The
    processing row is locked for the duration, so redelivered or concurrent
    executions for one Article serialize here and the later one is a no-op.
    """

    keys = current_keys()
    Article.objects.only("pk").get(pk=article_id)

    def run() -> StepResult:
        row = _row(article_id, keys)
        with transaction.atomic():
            row = ArticleStoryProcessing.objects.select_for_update().get(pk=row.pk)
            if _is_fresh(row, keys):
                return _result(row, story_id=_primary(article_id).story_id)
            current = _primary(article_id)
            if current is not None and (
                current.matcher_key != keys.matcher_key
                or current.evidence.get("embedding_model_key") != keys.embedding_model_key
            ):
                current.delete()
            outcome = match_article(article_id, model_key=keys.embedding_model_key)
            row.state = State.MATCHED
            row.embedding_model_key = keys.embedding_model_key
            row.matcher_key = keys.matcher_key
            row.attempts = 0
            row.error_kind = ""
            row.error_message = ""
            row.save()
        return _result(row, story_id=outcome.story_id)

    return _guarded(article_id, keys, run)


def process_article(article_id: int) -> StepResult:
    """Run both steps in this process (operator commands); stops at a failure."""

    embedded = embed_step(article_id)
    if embedded.state == State.FAILED:
        return embedded
    return match_step(article_id)


def reprocess_article(article_id: int) -> StepResult:
    """Delete this Article's derived Story rows, then process it again.

    Only derived state is removed — its embeddings, its primary association and
    its processing record. The Article and its provenance are never written.
    """

    Article.objects.only("pk").get(pk=article_id)
    with transaction.atomic():
        StoryArticle.objects.filter(article_id=article_id, is_primary=True).delete()
        ArticleEmbedding.objects.filter(article_id=article_id).delete()
        ArticleStoryProcessing.objects.filter(article_id=article_id).delete()
    return process_article(article_id)


def reconciliation_candidates(*, limit: int | None = None, now=None) -> list[int]:
    """Article ids whose Story state is missing, stale, stuck or retryable.

    Selected: no processing row; or a row older than the cutoff that is stale
    (either key differs from the configured pair), stuck in PENDING/EMBEDDED,
    FAILED below the attempt cap, or MATCHED without a primary association.
    Ordered by article id and capped at NEWS_STORY_RECONCILE_BATCH.
    """

    keys = current_keys()
    limit = settings.NEWS_STORY_RECONCILE_BATCH if limit is None else limit
    cutoff = (now or timezone.now()) - timedelta(
        seconds=settings.NEWS_STORY_RECONCILE_AFTER_SECONDS
    )
    same_pair = Q(
        story_processing__embedding_model_key=keys.embedding_model_key,
        story_processing__matcher_key=keys.matcher_key,
    )
    has_primary = Exists(StoryArticle.objects.filter(article_id=OuterRef("pk"), is_primary=True))
    due = Q(story_processing__updated_at__lte=cutoff) & (
        ~same_pair
        | Q(story_processing__state__in=[State.PENDING, State.EMBEDDED])
        | Q(
            story_processing__state=State.FAILED,
            story_processing__attempts__lt=settings.NEWS_STORY_PROCESSING_MAX_ATTEMPTS,
        )
        | Q(story_processing__state=State.MATCHED, has_primary=False)
    )
    return list(
        Article.objects.annotate(has_primary=has_primary)
        .filter(Q(story_processing__isnull=True) | due)
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )


def claim_for_reconciliation(article_ids: list[int]) -> None:
    """Record that these Articles were just re-dispatched.

    Missing rows are created PENDING for the configured pair and existing rows
    are touched, so the next sweep leaves them alone until the cutoff passes
    again instead of re-dispatching work that is still queued.
    """

    keys = current_keys()
    existing = set(
        ArticleStoryProcessing.objects.filter(article_id__in=article_ids).values_list(
            "article_id", flat=True
        )
    )
    ArticleStoryProcessing.objects.bulk_create(
        [
            ArticleStoryProcessing(
                article_id=article_id,
                embedding_model_key=keys.embedding_model_key,
                matcher_key=keys.matcher_key,
            )
            for article_id in article_ids
            if article_id not in existing
        ],
        ignore_conflicts=True,
    )
    ArticleStoryProcessing.objects.filter(article_id__in=existing).update(updated_at=timezone.now())
