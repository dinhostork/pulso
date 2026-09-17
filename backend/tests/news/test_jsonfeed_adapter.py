"""Offline JSON Feed mapping and hardened-fetcher integration tests."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from news.adapters.http import Fetcher
from news.adapters.jsonfeed import JSON_ACCEPT, JsonFeedAdapter, _raw
from news.adapters.rss import RssAdapter
from news.application.ports import (
    EndpointFetchRequest,
    FetchedItem,
    FetchError,
    FetchErrorKind,
    FetchResponse,
    FetchResult,
    SourceAdapter,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
URL = "https://feed.example/feed.json"


def fixture(name):
    return (FIXTURES / name).read_bytes()


def parse_body(body, *, content_type="application/feed+json", request=None):
    observed = []

    def handler(http_request):
        observed.append(http_request)
        return httpx.Response(
            200,
            headers={"Content-Type": content_type, "ETag": "v2", "Last-Modified": "today"},
            stream=httpx.ByteStream(body),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with Fetcher(client=client, resolver=lambda _host, _port: ("8.8.8.8",)) as fetcher:
            result = JsonFeedAdapter().fetch(
                request or EndpointFetchRequest(url=URL, etag="v1", last_modified="yesterday"),
                fetcher,
            )
    return result, observed


def test_valid_fixture_maps_shared_dtos_dates_and_validators():
    result, requests = parse_body(fixture("jsonfeed_valid.json"))
    assert isinstance(result, FetchResult)
    assert len(result.items) == 6
    assert all(isinstance(item, FetchedItem) for item in result.items)
    first = result.items[0]
    assert first.external_id == "story-1"
    assert first.url == "https://news.example/story/1?utm_source=fixture"
    assert first.title == "HTML story"
    assert first.content_html == "<p>HTML body</p>"
    assert first.summary_html == "Source summary"
    assert first.published_at == datetime(2026, 9, 17, 10, tzinfo=timezone.utc)
    assert first.updated_at == datetime(2026, 9, 17, 10, 30, 0, 125000, tzinfo=timezone.utc)
    assert first.authors == ("Author One", "Author Two")
    assert first.language == "pt-BR"
    assert result.items[1].content_html == "Plain source text"
    assert result.items[1].authors == ("Legacy Author",)
    assert result.items[1].language == result.feed_language == "en-US"
    assert result.items[2].external_id == "id-only" and result.items[2].url is None
    assert result.items[3].external_id is None
    assert result.items[3].url == "mailto:editor@news.example"
    assert result.items[4].published_at is result.items[4].updated_at is None
    assert result.items[4].raw["date_parse_error"] == "date_published,date_modified"
    assert [item.external_id for item in result.items].count("story-1") == 2
    assert result.etag == "v2" and result.last_modified == "today"
    assert requests[0].headers["accept"] == JSON_ACCEPT
    assert requests[0].headers["if-none-match"] == "v1"
    assert requests[0].headers["if-modified-since"] == "yesterday"


def test_raw_subset_is_json_safe_and_excludes_unknown_attachment_fields():
    result, _ = parse_body(fixture("jsonfeed_valid.json"))
    raw = result.items[0].raw
    assert raw == {
        "tags": ["Politics", "World"],
        "external_url": "mailto:source@news.example",
        "attachments": [
            {
                "url": "https://cdn.example/audio.mp3",
                "mime_type": "audio/mpeg",
                "title": "Audio",
                "size_in_bytes": 12345,
                "duration_in_seconds": 12.5,
            }
        ],
    }
    json.dumps(raw, allow_nan=False)
    assert "data" not in raw["attachments"][0]


def test_raw_metadata_is_explicitly_bounded():
    item = {
        "tags": ["x" * 5000] * 100,
        "external_url": "x" * 5000,
        "attachments": [
            {
                "url": "x" * 5000,
                "title": "x" * 5000,
                "size_in_bytes": float("inf"),
                "unknown": {"secret": "value"},
            }
        ]
        * 100,
    }
    raw = _raw(item, [])
    assert len(raw["tags"]) == len(raw["attachments"]) == 8
    assert len(raw["tags"][0]) == len(raw["external_url"]) == 256
    assert len(raw["attachments"][0]["url"]) == 256
    assert "unknown" not in raw["attachments"][0]
    assert "size_in_bytes" not in raw["attachments"][0]
    assert len(json.dumps(raw)) < 16 * 1024


@pytest.mark.parametrize(
    "body",
    [
        fixture("jsonfeed_malformed.json"),
        fixture("jsonfeed_missing_items.json"),
        b"[]",
        b'{"version":"https://example.org/other","items":[]}',
        b'{"version":"https://jsonfeed.org/version/1.1","items":{}}',
        b'{"version":"https://jsonfeed.org/version/1.1","items":[],"x":NaN}',
    ],
)
def test_malformed_documents_raise_safe_nonretryable_error(body):
    with pytest.raises(FetchError) as error:
        parse_body(body)
    assert error.value.kind == FetchErrorKind.MALFORMED
    assert not error.value.retryable
    assert body.decode("utf-8", errors="ignore") not in error.value.message


def test_empty_items_is_valid():
    result, _ = parse_body(b'{"version":"https://jsonfeed.org/version/1","items":[]}')
    assert result.items == result.rejected == ()


def test_items_are_isolated_and_rejected_with_original_positions():
    document = {
        "version": "https://jsonfeed.org/version/1.1",
        "items": [
            "not an object",
            {"id": {}, "title": "Nested identity"},
            {"title": "No identity"},
            {"id": "empty", "title": " ", "content_text": " "},
            {"id": "valid", "title": "Valid"},
        ],
    }
    result, _ = parse_body(json.dumps(document).encode())
    assert [item.external_id for item in result.items] == ["valid"]
    assert [(item.position, item.reason) for item in result.rejected] == [
        (0, "MALFORMED_ENTRY"),
        (1, "MALFORMED_ENTRY"),
        (2, "MISSING_IDENTITY"),
        (3, "EMPTY_ENTRY"),
    ]
    assert all(item.detail == "" for item in result.rejected)


def test_date_forms_are_aware_utc_and_do_not_fall_back():
    document = {
        "version": "https://jsonfeed.org/version/1.1",
        "items": [
            {"id": "z", "title": "Z", "date_published": "2026-09-17T10:00:00Z"},
            {"id": "offset", "title": "Offset", "date_published": "2026-09-17T07:00:00-03:00"},
            {
                "id": "fraction",
                "title": "Fraction",
                "date_modified": "2026-09-17T10:00:00.123456+00:00",
            },
        ],
    }
    result, _ = parse_body(json.dumps(document).encode())
    assert result.items[0].published_at == result.items[1].published_at
    assert result.items[2].published_at is None
    assert result.items[2].updated_at.microsecond == 123456
    assert all(
        value.tzinfo is timezone.utc
        for item in result.items
        for value in (item.published_at, item.updated_at)
        if value is not None
    )


def test_304_skips_json_parsing_and_returns_validators(monkeypatch):
    def no_loads(*args, **kwargs):
        raise AssertionError("JSON parser must not run")

    monkeypatch.setattr("news.adapters.jsonfeed.json.loads", no_loads)

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            return FetchResponse(not_modified=True, etag="v2", last_modified="today")

    result = JsonFeedAdapter().fetch(EndpointFetchRequest(url=URL), FakeFetcher())
    assert result.not_modified and result.items == result.rejected == ()
    assert (result.etag, result.last_modified) == ("v2", "today")


def test_transport_error_is_preserved():
    expected = FetchError(FetchErrorKind.TIMEOUT, "Upstream request timed out", retryable=True)

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            raise expected

    with pytest.raises(FetchError) as error:
        JsonFeedAdapter().fetch(EndpointFetchRequest(url=URL), FakeFetcher())
    assert error.value is expected


def test_credential_env_is_ignored_and_no_credential_header_is_added():
    request = EndpointFetchRequest(
        url=URL, adapter_config={"credential_env": "SOME_FAKE_SECRET_ENV"}
    )
    result, requests = parse_body(fixture("jsonfeed_valid.json"), request=request)
    assert result.items
    assert "authorization" not in requests[0].headers
    assert "api-key" not in requests[0].headers
    assert "token" not in requests[0].headers


def test_both_formats_satisfy_the_same_application_contract():
    assert isinstance(RssAdapter(), SourceAdapter)
    assert isinstance(JsonFeedAdapter(), SourceAdapter)
    json_result, _ = parse_body(fixture("jsonfeed_valid.json"))
    assert isinstance(json_result, FetchResult)
    assert isinstance(json_result.items[0], FetchedItem)
