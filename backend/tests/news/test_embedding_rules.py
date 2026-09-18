"""Pure embedding rules: input text, model identity, Story vectors and the test double."""

import hashlib
import math
import subprocess
import sys
from pathlib import Path

import pytest

from news.adapters.deterministic_embeddings import (
    DIMENSION,
    DeterministicEmbeddingProvider,
    deterministic_vector,
)
from news.application.story_ports import (
    MAX_EMBEDDING_DIMENSION,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingErrorKind,
    EmbeddingModel,
    EmbeddingProvider,
)
from news.domain.embeddings import article_embedding_input, story_vector


def test_input_is_title_then_description_then_body_with_blank_parts_omitted():
    text = article_embedding_input(
        title="  Council   approves\tbudget ",
        description="",
        body_text="The vote\nwas 7 to 2.",
        max_chars=1000,
    )
    assert text == "Council approves budget\n\nThe vote was 7 to 2."
    full = article_embedding_input(title="T", description="D", body_text="B", max_chars=1000)
    assert full == "T\n\nD\n\nB"


def test_input_truncation_is_deterministic_and_keeps_the_title_first():
    kwargs = {"title": "Headline", "description": "Summary " * 50, "body_text": "Body " * 500}
    first = article_embedding_input(**kwargs, max_chars=40)
    assert first == article_embedding_input(**kwargs, max_chars=40)
    assert first.startswith("Headline\n\nSummary")
    assert len(first) <= 40
    assert first == first.rstrip()
    assert article_embedding_input(**kwargs, max_chars=4) == "Head"
    with pytest.raises(ValueError):
        article_embedding_input(**kwargs, max_chars=0)


def test_model_key_is_derived_from_provider_model_and_revision():
    model = EmbeddingModel(provider="fastembed", model="org/name", revision="abc123", dimension=3)
    assert model.model_key == "fastembed:org/name@abc123"
    assert model == EmbeddingModel("fastembed", "org/name", "abc123", 3)
    with pytest.raises(AttributeError):
        model.revision = "other"


@pytest.mark.parametrize(
    "fields",
    [
        {"provider": ""},
        {"model": " padded"},
        {"revision": ""},
        {"dimension": 0},
        {"dimension": MAX_EMBEDDING_DIMENSION + 1},
        {"dimension": True},
        {"model": "m" * 300},
    ],
)
def test_model_identity_rejects_blank_or_unbounded_values(fields):
    values = {"provider": "p", "model": "m", "revision": "r", "dimension": 8} | fields
    with pytest.raises(EmbeddingError) as error:
        EmbeddingModel(**values)
    assert error.value.kind == EmbeddingErrorKind.INVALID_MODEL


def test_dimension_error_names_only_the_two_dimensions():
    error = EmbeddingDimensionError(384, 383, model_key="p:m@r")
    assert str(error) == "Embedding dimension mismatch: expected 384, received 383."
    assert (error.expected, error.received, error.model_key) == (384, 383, "p:m@r")


def test_story_vector_is_unit_length_mean_independent_of_member_order():
    members = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    vector = story_vector(members)
    assert vector == story_vector(list(reversed(members)))
    assert math.isclose(math.fsum(value * value for value in vector), 1.0)
    assert vector[0] == vector[1] == vector[2]
    for invalid in ([], [(1.0, 0.0), (1.0,)], [(1.0, 0.0), (-1.0, 0.0)]):
        with pytest.raises(ValueError):
            story_vector(invalid)


def test_deterministic_provider_preserves_order_and_repeats():
    provider = DeterministicEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)
    assert provider.identity.dimension == DIMENSION == 64
    texts = ["River flood closes bridge", "Budget vote delayed", "River flood closes bridge"]
    vectors = provider.embed(texts)
    assert len(vectors) == 3
    assert vectors[0] == vectors[2] != vectors[1]
    assert vectors == tuple(deterministic_vector(text) for text in texts)
    assert provider.embed(list(reversed(texts))) == tuple(reversed(vectors))
    for vector in vectors:
        assert len(vector) == DIMENSION
        assert math.isclose(math.fsum(value * value for value in vector), 1.0)


def test_deterministic_provider_places_shared_wording_closer():
    def cosine(a, b):
        return math.fsum(x * y for x, y in zip(a, b, strict=True))

    base, related, unrelated = DeterministicEmbeddingProvider().embed(
        [
            "harbor authority closes northern harbor after storm damage",
            "storm damage closes northern harbor says harbor authority",
            "museum opens exhibition of medieval maps",
        ]
    )
    assert cosine(base, related) > cosine(base, unrelated)


# Pinned digest of one vector: fails if the double ever depends on process state.
_PINNED = "Deterministic double stability check"


def _digest(vector):
    return hashlib.sha256(repr(vector).encode()).hexdigest()


def test_deterministic_vectors_are_stable_across_processes():
    here = _digest(deterministic_vector(_PINNED))
    script = (
        "import hashlib;"
        "from news.adapters.deterministic_embeddings import deterministic_vector as v;"
        f"print(hashlib.sha256(repr(v({_PINNED!r})).encode()).hexdigest())"
    )
    for seed in ("0", "12345"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": "."},
        )
        assert result.stdout.strip() == here
