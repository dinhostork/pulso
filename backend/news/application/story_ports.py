"""Infrastructure-independent contracts between Story use cases and adapters."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# The largest `vector` dimension pgvector can index (HNSW/IVFFlat), so every
# accepted model stays indexable when candidate retrieval needs it.
MAX_EMBEDDING_DIMENSION = 2000
MAX_MODEL_KEY_LENGTH = 255


class EmbeddingErrorKind(StrEnum):
    INVALID_MODEL = "INVALID_MODEL"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    DIMENSION_MISMATCH = "DIMENSION_MISMATCH"


class EmbeddingError(Exception):
    """Safe embedding failure metadata: never input text or provider objects."""

    def __init__(self, kind: EmbeddingErrorKind, message: str, *, model_key: str = ""):
        self.kind = kind
        self.message = " ".join(message.split())[:200]
        self.model_key = model_key
        super().__init__(self.message)


class EmbeddingDimensionError(EmbeddingError):
    """A vector length disagreed with the model identity or the stored rows."""

    def __init__(self, expected: int, received: int, *, model_key: str = ""):
        self.expected = expected
        self.received = received
        super().__init__(
            EmbeddingErrorKind.DIMENSION_MISMATCH,
            f"Embedding dimension mismatch: expected {expected}, received {received}.",
            model_key=model_key,
        )


@dataclass(frozen=True)
class EmbeddingModel:
    """Identity of the model that produced a vector; stored as `model_key`."""

    provider: str
    model: str
    revision: str
    dimension: int

    def __post_init__(self):
        for name in ("provider", "model", "revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise EmbeddingError(
                    EmbeddingErrorKind.INVALID_MODEL, f"Embedding model {name} must be nonempty."
                )
        if (
            not isinstance(self.dimension, int)
            or isinstance(self.dimension, bool)
            or not 1 <= self.dimension <= MAX_EMBEDDING_DIMENSION
        ):
            raise EmbeddingError(
                EmbeddingErrorKind.INVALID_MODEL,
                f"Embedding dimension must be from 1 to {MAX_EMBEDDING_DIMENSION}.",
            )
        if len(self.model_key) > MAX_MODEL_KEY_LENGTH:
            raise EmbeddingError(
                EmbeddingErrorKind.INVALID_MODEL,
                f"Embedding model key exceeds {MAX_MODEL_KEY_LENGTH} characters.",
            )

    @property
    def model_key(self) -> str:
        """`provider:model@revision`; vectors are only comparable within one key."""

        return f"{self.provider}:{self.model}@{self.revision}"


Vector = tuple[float, ...]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns already bounded texts into vectors, one per text, in input order."""

    @property
    def identity(self) -> EmbeddingModel: ...

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]: ...
