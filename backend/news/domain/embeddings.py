"""Deterministic embedding input and Story vector rules; no Django or models.

Article input is the normalized title, then description, then body text. Each
part has its whitespace collapsed, blank parts are omitted, and the parts are
joined by a blank line. The joined text is then cut to at most `max_chars`
code points and trailing whitespace is removed, so the title always leads and
the same Article always yields the same input.
"""

import math
from collections.abc import Sequence

PART_SEPARATOR = "\n\n"


def article_embedding_input(*, title: str, description: str, body_text: str, max_chars: int) -> str:
    """The bounded text a provider embeds for one Article."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    parts = [" ".join(part.split()) for part in (title, description, body_text)]
    joined = PART_SEPARATOR.join(part for part in parts if part)
    return joined[:max_chars].rstrip()


def story_vector(member_vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Unit-length element-wise mean of member Article vectors.

    `math.fsum` rounds exactly, so the result does not depend on member order.
    """

    if not member_vectors:
        raise ValueError("a Story vector needs at least one member vector")
    dimension = len(member_vectors[0])
    if any(len(vector) != dimension for vector in member_vectors):
        raise ValueError("member vectors must share one dimension")
    sums = [math.fsum(vector[index] for vector in member_vectors) for index in range(dimension)]
    norm = math.sqrt(math.fsum(value * value for value in sums))
    if norm == 0.0 or not math.isfinite(norm):
        raise ValueError("member vectors cancel out or are not finite")
    return tuple(value / norm for value in sums)
