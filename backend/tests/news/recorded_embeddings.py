"""Replay recorded local-model vectors for the Story evaluation corpus, offline.

The in-repository deterministic double (`news.adapters.deterministic_embeddings`)
hashes words, so it cannot tell a reworded report of one event from a
templated report of another; the #26 corpus is built to expose exactly that.
Matching quality is therefore measured with the real local model's vectors.

`recorded_local_embeddings.json` holds those vectors: one per corpus input
text, keyed by the SHA-256 of the exact text `embed_articles` sends, stored as
little-endian float32 in base64. They were produced by
`news.adapters.local_embeddings.LocalEmbeddingProvider` at its pinned revision.
`RecordedEmbeddingProvider` has the same identity, so rows it writes carry the
real `model_key`. It needs no model, no network and no optional dependency.
Any text that was not recorded fails instead of inventing a vector.

Re-record after changing the corpus text, the input rule or the pinned model:

    uv run --locked --group embeddings python -m tests.news.recorded_embeddings

The opt-in `local_embedding` test checks that the recording still matches the
live model.
"""

import base64
import hashlib
import json
import struct
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from news.adapters.local_embeddings import LocalEmbeddingProvider
from news.application.story_ports import EmbeddingModel, Vector

RECORDING_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "news"
    / "stories"
    / "recorded_local_embeddings.json"
)


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def decode(encoded: str) -> Vector:
    data = base64.b64decode(encoded)
    return tuple(struct.unpack(f"<{len(data) // 4}f", data))


@cache
def _recording() -> tuple[str, int, dict[str, Vector]]:
    document = json.loads(RECORDING_PATH.read_text(encoding="utf-8"))
    vectors = {key: decode(value) for key, value in document["vectors"].items()}
    return document["model_key"], document["dimension"], vectors


class RecordedEmbeddingProvider:
    identity: EmbeddingModel = LocalEmbeddingProvider.identity

    def __init__(self):
        model_key, dimension, self._vectors = _recording()
        if (model_key, dimension) != (self.identity.model_key, self.identity.dimension):
            raise ValueError("recorded vectors belong to a different local model")

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        return tuple(self._vectors[text_key(text)] for text in texts)


def corpus_inputs() -> list[str]:
    """The exact input text `embed_articles` builds for every corpus Article."""

    from django.conf import settings

    from news.domain.embeddings import article_embedding_input
    from tests.news.story_corpus import read_corpus

    return [
        article_embedding_input(
            title=article.title,
            description="",
            body_text=article.body,
            max_chars=settings.NEWS_EMBEDDING_MAX_INPUT_CHARS,
        )
        for article in read_corpus().articles
    ]


def record() -> dict:
    """Embed every corpus input with the live local model."""

    provider = LocalEmbeddingProvider()
    texts = corpus_inputs()
    vectors = provider.embed(texts)
    return {
        "model_key": provider.identity.model_key,
        "dimension": provider.identity.dimension,
        "vectors": {text_key(text): encode(vector) for text, vector in zip(texts, vectors)},
    }


if __name__ == "__main__":
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_test")
    django.setup()
    RECORDING_PATH.write_text(json.dumps(record(), indent=1, sort_keys=True) + "\n")
    print(f"recorded {len(corpus_inputs())} vectors to {RECORDING_PATH}")
