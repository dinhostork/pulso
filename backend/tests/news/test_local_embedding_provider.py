"""Opt-in check of the real local embedding model (not part of the default suite).

Requires the optional `embeddings` dependency group and a model downloaded by
the explicit step in backend/README.md. The session DNS guard stays active, so
this also proves the adapter embeds without any network access.
"""

import math

import pytest

pytestmark = pytest.mark.local_embedding


def _cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def test_local_model_embeds_offline_with_its_declared_identity():
    pytest.importorskip("fastembed")
    from news.adapters.local_embeddings import LocalEmbeddingProvider
    from news.application.embeddings import embed_texts

    provider = LocalEmbeddingProvider()
    same_event, reworded, unrelated = embed_texts(
        provider,
        [
            "Storm damage forces the northern harbor to close for repairs.",
            "Port authorities shut the north harbour after the storm damaged its piers.",
            "A museum opens an exhibition of medieval maps this weekend.",
        ],
    )

    assert provider.identity.model_key.startswith("fastembed:BAAI/bge-small-en-v1.5@")
    assert len(same_event) == provider.identity.dimension == 384
    assert _cosine(same_event, reworded) > _cosine(same_event, unrelated)
    assert embed_texts(provider, ["Storm damage forces the northern harbor to close for repairs."])[
        0
    ] == pytest.approx(same_event, abs=1e-5)
