"""Free, local sentence-embedding adapter (fastembed + ONNX Runtime).

The model runs on the CPU and transmits nothing. Weights are fetched once, by
an explicit download step pinned to one Hugging Face revision, into the
standard Hugging Face cache (`HF_HOME`); the adapter itself only ever loads
local files, so embedding never makes a network request. `fastembed` is an
optional dependency group and is imported lazily, so importing this module or
starting Django never needs it and never downloads anything.

Download (once), then check the model loads offline:

    uv run --locked --group embeddings python -m news.adapters.local_embeddings
"""

from collections.abc import Sequence

from news.application.story_ports import (
    EmbeddingError,
    EmbeddingErrorKind,
    EmbeddingModel,
    Vector,
)

MODEL = "BAAI/bge-small-en-v1.5"
# The quantized ONNX export fastembed publishes for MODEL.
SOURCE_REPOSITORY = "Qdrant/bge-small-en-v1.5-onnx-Q"
REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"
DIMENSION = 384
_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "model_optimized.onnx",
]


class LocalEmbeddingProvider:
    identity = EmbeddingModel(
        provider="fastembed", model=MODEL, revision=REVISION[:12], dimension=DIMENSION
    )

    def __init__(self, cache_dir: str | None = None):
        self._cache_dir = cache_dir
        self._model = None

    def _fail(self, message: str) -> EmbeddingError:
        return EmbeddingError(
            EmbeddingErrorKind.PROVIDER_FAILED, message, model_key=self.identity.model_key
        )

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
            from huggingface_hub import snapshot_download
        except ImportError:
            raise self._fail(
                "Local embeddings need the optional 'embeddings' dependency group."
            ) from None
        try:
            path = snapshot_download(
                repo_id=SOURCE_REPOSITORY,
                revision=REVISION,
                allow_patterns=_FILES,
                cache_dir=self._cache_dir,
                local_files_only=True,
            )
        except Exception:
            raise self._fail(
                "Local embedding model is not downloaded; run the explicit download step."
            ) from None
        try:
            self._model = TextEmbedding(model_name=MODEL, specific_model_path=path)
        except Exception:
            raise self._fail("Local embedding model could not be loaded.") from None
        return self._model

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        model = self._load()
        try:
            vectors = list(model.embed(list(texts), batch_size=max(len(texts), 1)))
        except Exception:
            raise self._fail("Local embedding model failed to embed the batch.") from None
        return tuple(tuple(float(value) for value in vector) for vector in vectors)


def download(cache_dir: str | None = None) -> str:
    """Fetch the pinned model files; the only network access this module makes."""

    from huggingface_hub import snapshot_download

    return snapshot_download(
        repo_id=SOURCE_REPOSITORY, revision=REVISION, allow_patterns=_FILES, cache_dir=cache_dir
    )


if __name__ == "__main__":
    download()
    provider = LocalEmbeddingProvider()
    (vector,) = provider.embed(["Local embedding model check."])
    print(f"model_key={provider.identity.model_key} dimension={len(vector)}")
