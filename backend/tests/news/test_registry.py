"""The application chooses concrete format adapters from persisted strings."""

import pytest

from news.adapters.jsonfeed import JsonFeedAdapter
from news.adapters.rss import RssAdapter
from news.application.registry import UnknownAdapterKind, adapter_for


def test_registry_returns_fresh_rss_adapter():
    first = adapter_for("RSS")
    second = adapter_for("RSS")
    assert isinstance(first, RssAdapter)
    assert first is not second


def test_registry_returns_json_feed_adapter():
    assert isinstance(adapter_for("JSON_FEED"), JsonFeedAdapter)


def test_registry_rejects_unknown_string_without_models():
    with pytest.raises(UnknownAdapterKind, match="X"):
        adapter_for("X")
