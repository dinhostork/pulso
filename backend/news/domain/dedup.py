"""Pure deterministic publication deduplication (ADR-0010).

Deduplication decides whether incoming normalized material is the publication
an existing Article already represents. It is not Story clustering: two
publications about one event are two Articles.

Precedence, evaluated in this exact order:

```text
1. (source, external_id) candidate
2. canonical_url candidate
3. content_fingerprint candidate (Tier 2)
4. CREATE
```

Content equality is only ever consulted when no publication identity candidate
exists, so it can never override an identity conflict. This module holds no
database, settings or framework access: candidates arrive as immutable
snapshots and the result is a value object the application layer applies.
"""

from dataclasses import dataclass
from enum import StrEnum

from .normalization import NormalizedArticle

#: Mirrors settings.NEWS_CONTENT_FINGERPRINT_MIN_CHARS. The application layer
#: passes the configured value; this default keeps the function callable and
#: correct on its own, without importing Django settings into the domain.
DEFAULT_MIN_FINGERPRINT_CHARS = 200


@dataclass(frozen=True)
class ArticleMatch:
    """Immutable snapshot of one candidate Article, free of ORM behavior."""

    id: int
    source_id: int
    canonical_url: str
    content_fingerprint: str


class DecisionKind(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    IDENTITY_DUPLICATE = "IDENTITY_DUPLICATE"
    CONTENT_DUPLICATE = "CONTENT_DUPLICATE"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    SOURCE_IDENTITY_CONFLICT = "SOURCE_IDENTITY_CONFLICT"


@dataclass(frozen=True)
class Decision:
    """A dedup outcome and the Articles the application layer needs.

    `article` carries the single Article the decision refers to: the update
    target, the unchanged identity duplicate, the earliest content duplicate to
    link, the Source that owns the canonical URL, or, for `IDENTITY_CONFLICT`,
    the `(source, external_id)` match. `conflicting` is set only for
    `IDENTITY_CONFLICT` and carries the canonical-URL match. Both are snapshots;
    no Article is ever mutated by a decision.
    """

    kind: DecisionKind
    article: ArticleMatch | None = None
    conflicting: ArticleMatch | None = None


def decide(
    normalized: NormalizedArticle,
    *,
    source_id: int,
    by_external_id: ArticleMatch | None,
    by_canonical_url: ArticleMatch | None,
    by_fingerprint: ArticleMatch | None,
    min_fingerprint_chars: int = DEFAULT_MIN_FINGERPRINT_CHARS,
) -> Decision:
    """Classify one normalized revision against its candidate Articles.

    `by_external_id` is the `(source_id, external_id)` match and therefore
    always belongs to the incoming Source; provider ids never match across
    Sources. `by_canonical_url` is the global owner of the canonical URL and may
    belong to another Source. `by_fingerprint` is the earliest Article sharing
    the exact content fingerprint. Callers may pass `None` for a candidate they
    did not query, which the precedence above makes unobservable.
    """

    if by_external_id is not None:
        if by_canonical_url is not None and by_canonical_url.id != by_external_id.id:
            # The URL this revision claims belongs to a different publication.
            # Cross-Source ownership is reported as such and takes precedence:
            # neither Article may be updated to resolve the collision.
            if by_canonical_url.source_id != source_id:
                return Decision(DecisionKind.SOURCE_IDENTITY_CONFLICT, article=by_canonical_url)
            return Decision(
                DecisionKind.IDENTITY_CONFLICT,
                article=by_external_id,
                conflicting=by_canonical_url,
            )
        # Same provider id with a new canonical URL is a revision, not a
        # duplicate, even when the content text did not change.
        if (
            by_external_id.canonical_url == normalized.canonical_url
            and by_external_id.content_fingerprint == normalized.content_fingerprint
        ):
            return Decision(DecisionKind.IDENTITY_DUPLICATE, article=by_external_id)
        return Decision(DecisionKind.UPDATE, article=by_external_id)

    if by_canonical_url is not None:
        if by_canonical_url.source_id != source_id:
            return Decision(DecisionKind.SOURCE_IDENTITY_CONFLICT, article=by_canonical_url)
        if by_canonical_url.content_fingerprint == normalized.content_fingerprint:
            return Decision(DecisionKind.IDENTITY_DUPLICATE, article=by_canonical_url)
        return Decision(DecisionKind.UPDATE, article=by_canonical_url)

    # Tier 2: exact content equality under a different canonical URL is a
    # republication of real, separate provenance. Short material is not
    # evidence of anything, so it stays below the threshold and creates.
    if (
        by_fingerprint is not None
        and by_fingerprint.canonical_url != normalized.canonical_url
        and normalized.fingerprint_input_length >= min_fingerprint_chars
    ):
        return Decision(DecisionKind.CONTENT_DUPLICATE, article=by_fingerprint)

    return Decision(DecisionKind.CREATE)
