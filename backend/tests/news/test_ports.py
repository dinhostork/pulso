"""Application boundary contracts remain immutable and infrastructure-free."""

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from news.application.ports import (
    EndpointFetchRequest,
    FetchedItem,
    FetchError,
    FetchErrorKind,
    FetchResponse,
    FetchResult,
    RejectedItem,
    SourceAdapter,
)


def test_dtos_are_frozen_and_hold_expected_fields():
    item = FetchedItem(external_id="guid", url=None, title="Title")
    result = FetchResult(items=(item,), rejected=(), feed_language="en")
    request = EndpointFetchRequest(url="https://example.com/feed", adapter_config={})
    assert result.items == (item,)
    assert request.etag is None
    assert item.authors == ()
    for value in (item, result, request):
        with pytest.raises(FrozenInstanceError):
            value.url = "other"


def test_rejected_item_detail_is_bounded_and_single_line():
    item = RejectedItem(position=3, reason="MISSING_IDENTITY", detail=" x\n" + "a" * 300)
    assert item.position == 3
    assert "\n" not in item.detail
    assert len(item.detail) == 200


def test_fetch_error_has_only_operational_fields():
    error = FetchError(
        FetchErrorKind.RATE_LIMITED,
        "Upstream rate limited the request",
        retryable=True,
        http_status=429,
        retry_after=120,
    )
    assert error.kind == FetchErrorKind.RATE_LIMITED
    assert error.retryable
    assert error.http_status == 429
    assert error.retry_after == 120
    assert str(error) == error.message
    assert {kind.value for kind in FetchErrorKind} == {
        "NETWORK",
        "TIMEOUT",
        "HTTP_STATUS",
        "RATE_LIMITED",
        "TOO_LARGE",
        "UNSUPPORTED_CONTENT",
        "MALFORMED",
        "BLOCKED_TARGET",
        "TOO_MANY_REDIRECTS",
    }


def test_fetch_response_exposes_only_parser_headers():
    response = FetchResponse(body=b"feed", content_type="application/rss+xml", etag="tag")
    assert response.response_headers == {"content-type": "application/rss+xml"}


def test_source_adapter_is_structural():
    class FakeAdapter:
        kind = "RSS"

        def fetch(self, request, fetcher):
            return FetchResult()

    assert isinstance(FakeAdapter(), SourceAdapter)


def test_application_contract_has_no_framework_or_transport_imports():
    path = Path(__file__).resolve().parents[2] / "news" / "application" / "ports.py"
    tree = ast.parse(path.read_text())
    forbidden = {"django", "httpx", "feedparser", "news.models"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
        else:
            continue
        assert all(
            not any(name == banned or name.startswith(banned + ".") for banned in forbidden)
            for name in imports
        )


def test_http_adapter_does_not_import_django_models():
    path = Path(__file__).resolve().parents[2] / "news" / "adapters" / "http.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"news.models", "django.db.models"}
