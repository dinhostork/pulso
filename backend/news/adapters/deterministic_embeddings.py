"""Offline, repeatable embedding test double used by the default test suite.

Each lowercase word is hashed with SHA-256 into one of `DIMENSION` buckets with
a sign, and the bucket counts are scaled to unit length. Texts that share words
therefore land closer together, which keeps similarity-based tests meaningful,
while the vector depends only on the text: no network, no model file, and no
use of Python's per-process randomized `hash()`.
"""

import hashlib
import math
import re
from collections.abc import Sequence

from news.application.story_ports import EmbeddingModel, Vector

DIMENSION = 64
_WORD = re.compile(r"\w+")


def _bucket(token: str) -> tuple[int, float]:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % DIMENSION, 1.0 if digest[4] & 1 else -1.0


def deterministic_vector(text: str) -> Vector:
    counts = [0.0] * DIMENSION
    for token in _WORD.findall(text.lower()) or [text]:
        index, sign = _bucket(token)
        counts[index] += sign
    norm = math.sqrt(math.fsum(value * value for value in counts))
    if norm == 0.0:
        # Opposite-signed words cancelled out; fall back to the whole text.
        index, sign = _bucket(text)
        counts = [0.0] * DIMENSION
        counts[index] = sign
        norm = 1.0
    return tuple(value / norm for value in counts)


class DeterministicEmbeddingProvider:
    identity = EmbeddingModel(
        provider="deterministic", model="sha256-token-hash", revision="1", dimension=DIMENSION
    )

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        return tuple(deterministic_vector(text) for text in texts)
