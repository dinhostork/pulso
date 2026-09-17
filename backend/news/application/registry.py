"""Explicit News source-adapter selection by persisted kind string."""

from news.adapters.jsonfeed import JsonFeedAdapter
from news.adapters.rss import RssAdapter
from news.application.ports import SourceAdapter


class UnknownAdapterKind(ValueError):
    """No source adapter handles the requested kind."""


def adapter_for(kind: str) -> SourceAdapter:
    """Return a fresh adapter without loading models or discovering plugins."""

    if kind == "RSS":
        return RssAdapter()
    if kind == "JSON_FEED":
        return JsonFeedAdapter()
    raise UnknownAdapterKind(f"Unknown source adapter kind: {kind}")
