"""Explicit News adapter and embedding-provider selection by configured name."""

from news.adapters.deterministic_embeddings import DeterministicEmbeddingProvider
from news.adapters.jsonfeed import JsonFeedAdapter
from news.adapters.local_embeddings import LocalEmbeddingProvider
from news.adapters.rss import RssAdapter
from news.application.ports import SourceAdapter
from news.application.story_ports import EmbeddingProvider


class UnknownAdapterKind(ValueError):
    """No source adapter handles the requested kind."""


class UnknownEmbeddingProvider(ValueError):
    """No embedding provider has the configured name."""


def adapter_for(kind: str) -> SourceAdapter:
    """Return a fresh adapter without loading models or discovering plugins."""

    if kind == "RSS":
        return RssAdapter()
    if kind == "JSON_FEED":
        return JsonFeedAdapter()
    raise UnknownAdapterKind(f"Unknown source adapter kind: {kind}")


def embedding_provider_for(name: str) -> EmbeddingProvider:
    """Return a fresh provider; the local model is loaded lazily on first use."""

    if name == "deterministic":
        return DeterministicEmbeddingProvider()
    if name == "local":
        return LocalEmbeddingProvider()
    raise UnknownEmbeddingProvider(f"Unknown embedding provider: {name}")
