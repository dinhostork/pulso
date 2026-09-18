"""Derived embedding persistence against real PostgreSQL/pgvector (issue #25)."""

import socket
import sys

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils import timezone
from pgvector.django import CosineDistance, L2Distance

from news.adapters.deterministic_embeddings import (
    DeterministicEmbeddingProvider,
    deterministic_vector,
)
from news.adapters.local_embeddings import LocalEmbeddingProvider
from news.application.embeddings import (
    configured_provider,
    embed_article,
    embed_articles,
    embed_story,
    embed_texts,
)
from news.application.registry import UnknownEmbeddingProvider, embedding_provider_for
from news.application.story_ports import (
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingErrorKind,
    EmbeddingModel,
)
from news.domain.embeddings import article_embedding_input, story_vector
from news.models import (
    Article,
    ArticleEmbedding,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
)

SECRET_TEXT = "DO_NOT_LEAK_THIS_ARTICLE_TEXT"


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    """Endpoint validation resolves names, so persistence tests use fake DNS."""

    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


class RecordingProvider:
    """Wraps a provider, recording each batch it is asked to embed."""

    def __init__(self, inner=None, *, identity=None, transform=None):
        self.inner = inner or DeterministicEmbeddingProvider()
        self.identity = identity or self.inner.identity
        self.transform = transform
        self.batches = []

    def embed(self, texts):
        self.batches.append(list(texts))
        vectors = self.inner.embed(texts)
        return self.transform(vectors) if self.transform else vectors


def other_model(dimension=64, revision="2"):
    return EmbeddingModel("deterministic", "sha256-token-hash", revision, dimension)


_counter = iter(range(1, 10_000))


def make_article(*, title="Harbor closes after storm", description="", body_text=""):
    number = next(_counter)
    source = Source.objects.create(slug=f"source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed.xml"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": title},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=title,
        description=description,
        body_text=body_text,
        language="en",
        content_fingerprint=f"{number:064d}",
        first_seen_at=timezone.now(),
    )


def make_story(*articles):
    story = Story.objects.create(language="en")
    for index, article in enumerate(articles):
        StoryArticle.objects.create(
            story=story,
            article=article,
            is_primary=True,
            method=StoryArticle.Method.CREATED_STORY if index == 0 else StoryArticle.Method.MATCHED,
        )
    return story


def stored_vectors(table):
    """The vectors exactly as PostgreSQL stores them, keyed by owner id."""

    owner = "article_id" if table == "news_articleembedding" else "story_id"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {owner}, model_key, vector::text FROM {table} ORDER BY 1, 2")
        return cursor.fetchall()


@pytest.mark.django_db
def test_same_article_and_model_key_embeds_once():
    article = make_article(body_text="The northern harbor closed on Tuesday.")
    provider = RecordingProvider()

    first = embed_article(article.pk, provider)
    second = embed_article(article.pk, provider)

    assert second.pk == first.pk
    assert second.generated_at == first.generated_at
    assert len(provider.batches) == 1
    assert ArticleEmbedding.objects.filter(article=article).count() == 1
    assert first.model_key == "deterministic:sha256-token-hash@1"
    assert first.dimension == 64
    assert len(first.vector) == 64
    assert first.input_chars == len(
        "Harbor closes after storm\n\nThe northern harbor closed on Tuesday."
    )


@pytest.mark.django_db
def test_second_model_key_creates_a_separate_row_and_keeps_the_first():
    article = make_article()
    first = embed_article(article.pk, DeterministicEmbeddingProvider())
    second = embed_article(article.pk, RecordingProvider(identity=other_model()))

    assert second.pk != first.pk
    assert set(article.embeddings.values_list("model_key", flat=True)) == {
        "deterministic:sha256-token-hash@1",
        "deterministic:sha256-token-hash@2",
    }
    assert ArticleEmbedding.objects.filter(pk=first.pk).exists()


@pytest.mark.django_db
def test_every_row_carries_a_model_key_and_dimension_enforced_by_the_database():
    article = make_article()
    embed_article(article.pk, DeterministicEmbeddingProvider())
    embed_story(make_story(article).pk, DeterministicEmbeddingProvider())

    for model in (ArticleEmbedding, StoryEmbedding):
        assert not model.objects.filter(model_key="").exists()
        assert all(row.dimension == len(row.vector) for row in model.objects.all())

    base = {"article": article, "vector": [1.0, 0.0], "input_chars": 5}
    for fields in (
        {"model_key": "", "dimension": 2},
        {"model_key": "manual:length-check@1", "dimension": 3},
        {"model_key": "manual:range-check@1", "dimension": 2001, "vector": [0.5] * 2001},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            ArticleEmbedding.objects.create(**(base | fields))


@pytest.mark.django_db
@pytest.mark.parametrize("received", [63, 65])
def test_wrong_vector_length_fails_explicitly_and_stores_nothing(received):
    article = make_article(body_text=SECRET_TEXT)
    story = make_story(article)
    provider = RecordingProvider(
        transform=lambda vectors: tuple((vector + (0.5,))[:received] for vector in vectors)
    )

    for call in (
        lambda: embed_article(article.pk, provider),
        lambda: embed_story(story.pk, provider),
    ):
        with pytest.raises(EmbeddingDimensionError) as error:
            call()
        assert (
            str(error.value) == f"Embedding dimension mismatch: expected 64, received {received}."
        )
        assert error.value.model_key == provider.identity.model_key
        assert SECRET_TEXT not in str(error.value)
    assert not ArticleEmbedding.objects.exists()
    assert not StoryEmbedding.objects.exists()


@pytest.mark.django_db
def test_dimension_disagreeing_with_stored_rows_for_the_model_key_fails():
    first, second = make_article(), make_article(title="Budget vote delayed")
    embed_article(first.pk, DeterministicEmbeddingProvider())
    same_key_new_size = RecordingProvider(
        identity=EmbeddingModel("deterministic", "sha256-token-hash", "1", 32),
        transform=lambda vectors: tuple(vector[:32] for vector in vectors),
    )

    with pytest.raises(EmbeddingDimensionError) as error:
        embed_article(second.pk, same_key_new_size)
    assert (error.value.expected, error.value.received) == (64, 32)
    assert same_key_new_size.batches == []
    assert not second.embeddings.exists()


@pytest.mark.django_db
def test_embedding_leaves_every_article_and_raw_article_column_unchanged():
    articles = [
        make_article(description="Summary " + SECRET_TEXT, body_text="Body " * 900),
        make_article(title="Budget vote delayed"),
    ]
    before = (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    )

    embed_articles([article.pk for article in articles], DeterministicEmbeddingProvider())
    embed_story(make_story(*articles).pk, RecordingProvider(identity=other_model()))

    after = (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    )
    assert after == before


@pytest.mark.django_db
def test_deleted_embeddings_rebuild_to_identical_vectors():
    articles = [make_article(title=title) for title in ("Harbor closes", "Harbor reopens")]
    story = make_story(*articles)
    provider = DeterministicEmbeddingProvider()
    embed_articles([article.pk for article in articles], provider)
    embed_story(story.pk, provider)
    before = (stored_vectors("news_articleembedding"), stored_vectors("news_storyembedding"))

    StoryEmbedding.objects.all().delete()
    ArticleEmbedding.objects.all().delete()
    embed_story(story.pk, provider)

    after = (stored_vectors("news_articleembedding"), stored_vectors("news_storyembedding"))
    assert after == before
    assert len(before[0]) == 2 and len(before[1]) == 1


@pytest.mark.django_db
def test_story_embedding_is_the_mean_of_members_and_embeds_once():
    articles = [make_article(title=title) for title in ("Harbor closes", "Storm hits harbor")]
    story = make_story(*articles)
    provider = RecordingProvider()

    first = embed_story(story.pk, provider)
    second = embed_story(story.pk, provider)

    assert second.pk == first.pk and second.generated_at == first.generated_at
    assert len(provider.batches) == 1
    assert first.member_count == 2
    assert first.dimension == 64
    members = [
        ArticleEmbedding.objects.get(article=article, model_key=first.model_key).vector
        for article in articles
    ]
    expected = [float(f"{value:.9g}") for value in story_vector(members)]
    assert first.vector == pytest.approx(expected, abs=1e-6)


@pytest.mark.django_db
def test_story_without_members_has_no_embedding():
    story = Story.objects.create(language="en")
    with pytest.raises(EmbeddingError) as error:
        embed_story(story.pk, DeterministicEmbeddingProvider())
    assert error.value.kind == EmbeddingErrorKind.INVALID_INPUT
    assert not StoryEmbedding.objects.exists()


@pytest.mark.django_db
def test_embedding_rows_follow_derived_state_deletion_rules():
    article = make_article()
    story = make_story(article)
    embed_story(story.pk, DeterministicEmbeddingProvider())

    story.delete()
    assert not StoryEmbedding.objects.exists()
    assert ArticleEmbedding.objects.filter(article=article).exists()
    with pytest.raises(ProtectedError):
        article.delete()


@pytest.mark.django_db
def test_pgvector_distance_query_orders_in_postgresql():
    key = "manual:distance-check@1"
    vectors = {
        "exact": [1.0, 0.0, 0.0],
        "near": [0.9, 0.1, 0.0],
        "far": [0.0, 1.0, 0.0],
        "opposite": [-1.0, 0.0, 0.0],
    }
    ids = {}
    for name, vector in vectors.items():
        row = ArticleEmbedding.objects.create(
            article=make_article(title=name),
            model_key=key,
            dimension=3,
            vector=vector,
            input_chars=1,
        )
        ids[row.pk] = name
    query = [1.0, 0.0, 0.0]

    for distance in (CosineDistance, L2Distance):
        ordered = (
            ArticleEmbedding.objects.filter(model_key=key)
            .annotate(distance=distance("vector", query))
            .order_by("distance", "pk")
        )
        assert [ids[row.pk] for row in ordered] == ["exact", "near", "far", "opposite"]
        assert "<=>" in str(ordered.query) or "<->" in str(ordered.query)


@pytest.mark.django_db
def test_semantic_neighbors_from_the_default_provider_rank_first():
    provider = DeterministicEmbeddingProvider()
    base = make_article(title="Storm damage closes northern harbor")
    related = make_article(title="Northern harbor closed by storm damage")
    unrelated = make_article(title="Museum opens medieval map exhibition")
    embed_articles([base.pk, related.pk, unrelated.pk], provider)

    query = ArticleEmbedding.objects.get(article=base).vector
    ordered = (
        ArticleEmbedding.objects.exclude(article=base)
        .annotate(distance=CosineDistance("vector", query))
        .order_by("distance", "pk")
    )
    assert [row.article_id for row in ordered] == [related.pk, unrelated.pk]


@pytest.mark.django_db
@override_settings(NEWS_EMBEDDING_MAX_BATCH=2)
def test_batches_are_bounded_and_rows_keep_input_order():
    articles = [make_article(title=f"Headline number {index}") for index in range(5)]
    provider = RecordingProvider()

    rows = embed_articles([article.pk for article in reversed(articles)], provider)

    assert [len(batch) for batch in provider.batches] == [2, 2, 1]
    for article in articles:
        text = article_embedding_input(
            title=article.title, description="", body_text="", max_chars=2000
        )
        assert rows[article.pk].vector == pytest.approx(list(deterministic_vector(text)), abs=1e-6)


@pytest.mark.django_db
@override_settings(NEWS_EMBEDDING_MAX_INPUT_CHARS=30)
def test_input_is_cut_to_the_configured_bound():
    article = make_article(body_text=SECRET_TEXT * 20)
    provider = RecordingProvider()

    row = embed_article(article.pk, provider)

    (batch,) = provider.batches
    assert len(batch[0]) <= 30
    assert row.input_chars == len(batch[0])
    assert batch[0] == article_embedding_input(
        title=article.title, description="", body_text=article.body_text, max_chars=30
    )


@override_settings(NEWS_EMBEDDING_MAX_INPUT_CHARS=10)
def test_embed_texts_refuses_unbounded_or_blank_input_without_echoing_it():
    provider = RecordingProvider()
    for texts in ([SECRET_TEXT], ["   "]):
        with pytest.raises(EmbeddingError) as error:
            embed_texts(provider, texts)
        assert error.value.kind == EmbeddingErrorKind.INVALID_INPUT
        assert SECRET_TEXT not in str(error.value)
    assert provider.batches == []


@pytest.mark.parametrize(
    "transform",
    [
        lambda vectors: vectors[:-1],
        lambda vectors: tuple((float("nan"),) + vector[1:] for vector in vectors),
        lambda vectors: tuple(("x",) + vector[1:] for vector in vectors),
    ],
)
def test_embed_texts_rejects_malformed_provider_output(transform):
    with pytest.raises(EmbeddingError) as error:
        embed_texts(RecordingProvider(transform=transform), ["one", "two"])
    assert error.value.kind == EmbeddingErrorKind.INVALID_OUTPUT


def test_provider_failures_are_wrapped_without_input_or_library_context():
    class Failing:
        identity = DeterministicEmbeddingProvider.identity

        def embed(self, texts):
            raise RuntimeError(f"library failure while embedding {texts!r}")

    with pytest.raises(EmbeddingError) as error:
        embed_texts(Failing(), [SECRET_TEXT])
    assert error.value.kind == EmbeddingErrorKind.PROVIDER_FAILED
    assert error.value.model_key == "deterministic:sha256-token-hash@1"
    assert SECRET_TEXT not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_settings_select_the_offline_provider_and_names_are_explicit():
    assert isinstance(configured_provider(), DeterministicEmbeddingProvider)
    assert isinstance(embedding_provider_for("local"), LocalEmbeddingProvider)
    with pytest.raises(UnknownEmbeddingProvider):
        embedding_provider_for("hosted")


def test_selecting_the_local_provider_loads_and_downloads_nothing():
    already_loaded = "fastembed" in sys.modules
    provider = embedding_provider_for("local")
    assert provider.identity.model_key.startswith("fastembed:BAAI/bge-small-en-v1.5@")
    assert provider.identity.dimension == 384
    assert ("fastembed" in sys.modules) == already_loaded


def test_default_suite_cannot_reach_a_model_host():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("huggingface.co", 443)
