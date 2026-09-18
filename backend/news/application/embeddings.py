"""Generate and store derived Article and Story embeddings idempotently.

Bounds are enforced here, once, before any provider call: each text is at most
NEWS_EMBEDDING_MAX_INPUT_CHARS and each provider call receives at most
NEWS_EMBEDDING_MAX_BATCH texts. Every returned vector is checked against the
provider's identity and never truncated or padded. Rows are keyed by
(`article`/`story`, `model_key`); an existing row is returned unchanged, and a
concurrent insert is resolved by PostgreSQL's uniqueness and re-read.
Embedding never writes to `Article`, `RawArticle`, `Story` or `StoryArticle`.
"""

import math
from collections.abc import Sequence

from django.conf import settings
from django.db import IntegrityError, transaction

from news.application.registry import embedding_provider_for
from news.application.story_ports import (
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingErrorKind,
    EmbeddingModel,
    EmbeddingProvider,
    Vector,
)
from news.domain.embeddings import article_embedding_input, story_vector
from news.models import Article, ArticleEmbedding, Story, StoryArticle, StoryEmbedding


def configured_provider() -> EmbeddingProvider:
    """The provider named by NEWS_EMBEDDING_PROVIDER."""

    return embedding_provider_for(settings.NEWS_EMBEDDING_PROVIDER)


def _checked_vector(vector: Sequence[float], identity: EmbeddingModel) -> Vector:
    if len(vector) != identity.dimension:
        raise EmbeddingDimensionError(identity.dimension, len(vector), model_key=identity.model_key)
    try:
        values = tuple(float(value) for value in vector)
    except TypeError, ValueError:
        values = ()
    if len(values) != identity.dimension or not all(math.isfinite(value) for value in values):
        raise EmbeddingError(
            EmbeddingErrorKind.INVALID_OUTPUT,
            "Embedding provider returned a non-numeric or non-finite value.",
            model_key=identity.model_key,
        )
    return values


def embed_texts(provider: EmbeddingProvider, texts: Sequence[str]) -> tuple[Vector, ...]:
    """Embed bounded texts in bounded batches; one checked vector per text, in order."""

    identity = provider.identity
    max_chars = settings.NEWS_EMBEDDING_MAX_INPUT_CHARS
    max_batch = settings.NEWS_EMBEDDING_MAX_BATCH
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError(
                EmbeddingErrorKind.INVALID_INPUT,
                "Embedding input must be nonempty text.",
                model_key=identity.model_key,
            )
        if len(text) > max_chars:
            raise EmbeddingError(
                EmbeddingErrorKind.INVALID_INPUT,
                f"Embedding input exceeds {max_chars} characters.",
                model_key=identity.model_key,
            )
    vectors: list[Vector] = []
    for start in range(0, len(texts), max_batch):
        batch = list(texts[start : start + max_batch])
        try:
            returned = provider.embed(batch)
        except EmbeddingError:
            raise
        except TimeoutError, ConnectionError:
            raise EmbeddingError(
                EmbeddingErrorKind.PROVIDER_UNAVAILABLE,
                "Embedding provider is temporarily unavailable.",
                model_key=identity.model_key,
                retryable=True,
            ) from None
        except Exception:
            # Third-party errors may carry inputs or payloads; keep none of it.
            raise EmbeddingError(
                EmbeddingErrorKind.PROVIDER_FAILED,
                "Embedding provider failed.",
                model_key=identity.model_key,
            ) from None
        if len(returned) != len(batch):
            raise EmbeddingError(
                EmbeddingErrorKind.INVALID_OUTPUT,
                f"Embedding provider returned {len(returned)} vectors for {len(batch)} inputs.",
                model_key=identity.model_key,
            )
        vectors.extend(_checked_vector(vector, identity) for vector in returned)
    return tuple(vectors)


def _check_stored_dimension(identity: EmbeddingModel) -> None:
    """Refuse a provider whose dimension disagrees with rows already stored for its key."""

    for model in (ArticleEmbedding, StoryEmbedding):
        stored = (
            model.objects.filter(model_key=identity.model_key)
            .exclude(dimension=identity.dimension)
            .values_list("dimension", flat=True)
            .first()
        )
        if stored is not None:
            raise EmbeddingDimensionError(stored, identity.dimension, model_key=identity.model_key)


def _create_once(model, lookup: dict, fields: dict):
    """Insert one derived row, or return the row a concurrent writer committed."""

    try:
        with transaction.atomic():
            return model.objects.create(**lookup, **fields)
    except IntegrityError:
        existing = model.objects.filter(**lookup).first()
        if existing is None:
            raise
        return existing


def embed_articles(
    article_ids: Sequence[int], provider: EmbeddingProvider
) -> dict[int, ArticleEmbedding]:
    """Ensure one ArticleEmbedding per Article for the provider's `model_key`."""

    identity = provider.identity
    key = identity.model_key
    wanted = sorted(set(article_ids))
    rows = {
        row.article_id: row
        for row in ArticleEmbedding.objects.filter(article_id__in=wanted, model_key=key)
    }
    missing = [article_id for article_id in wanted if article_id not in rows]
    if not missing:
        return rows

    _check_stored_dimension(identity)
    articles = Article.objects.filter(pk__in=missing).only("title", "description", "body_text")
    by_id = {article.pk: article for article in articles}
    if len(by_id) != len(missing):
        raise Article.DoesNotExist("Article to embed does not exist.")
    max_chars = settings.NEWS_EMBEDDING_MAX_INPUT_CHARS
    texts = [
        article_embedding_input(
            title=by_id[article_id].title,
            description=by_id[article_id].description,
            body_text=by_id[article_id].body_text,
            max_chars=max_chars,
        )
        for article_id in missing
    ]
    vectors = embed_texts(provider, texts)
    for article_id, text, vector in zip(missing, texts, vectors, strict=True):
        rows[article_id] = _create_once(
            ArticleEmbedding,
            {"article_id": article_id, "model_key": key},
            {"dimension": identity.dimension, "vector": list(vector), "input_chars": len(text)},
        )
    return rows


def embed_article(article_id: int, provider: EmbeddingProvider) -> ArticleEmbedding:
    """Return the Article's embedding for the provider's `model_key`, creating it once."""

    return embed_articles([article_id], provider)[article_id]


def embed_story(story_id: int, provider: EmbeddingProvider) -> StoryEmbedding:
    """Return the Story's embedding for the provider's `model_key`, creating it once.

    The vector is the unit-length mean of the current member Articles' vectors
    (see `news.domain.embeddings.story_vector`); members are embedded first
    when needed. Refreshing an existing row as membership changes is not done
    here.
    """

    identity = provider.identity
    key = identity.model_key
    existing = StoryEmbedding.objects.filter(story_id=story_id, model_key=key).first()
    if existing is not None:
        return existing
    if not Story.objects.filter(pk=story_id).exists():
        raise Story.DoesNotExist("Story to embed does not exist.")
    member_ids = sorted(
        StoryArticle.objects.filter(story_id=story_id).values_list("article_id", flat=True)
    )
    if not member_ids:
        raise EmbeddingError(
            EmbeddingErrorKind.INVALID_INPUT, "Story has no member Articles.", model_key=key
        )
    members = embed_articles(member_ids, provider)
    try:
        vector = story_vector([members[article_id].vector for article_id in member_ids])
    except ValueError:
        raise EmbeddingError(
            EmbeddingErrorKind.INVALID_OUTPUT,
            "Member vectors cannot be combined into a Story vector.",
            model_key=key,
        ) from None
    _checked_vector(vector, identity)
    return _create_once(
        StoryEmbedding,
        {"story_id": story_id, "model_key": key},
        {"dimension": identity.dimension, "vector": list(vector), "member_count": len(member_ids)},
    )
