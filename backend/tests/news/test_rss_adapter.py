"""Offline syndication fixtures exercise the adapter and hardened fetcher boundary."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from news.adapters.http import Fetcher
from news.adapters.rss import RSS_ACCEPT, RssAdapter, _content, _raw
from news.application.ports import (
    EndpointFetchRequest,
    FetchError,
    FetchErrorKind,
    FetchResponse,
    SourceAdapter,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
URL = "https://feed.example/syndication"


def fixture(name):
    return (FIXTURES / name).read_bytes()


def parsed_fixture(name, *, content_type="application/rss+xml", status=200, headers=None):
    body = fixture(name)
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(
            status,
            headers={"Content-Type": content_type, "ETag": "v2", **(headers or {})},
            stream=httpx.ByteStream(body),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with Fetcher(client=client, resolver=lambda _host, _port: ("8.8.8.8",)) as fetcher:
            result = RssAdapter().fetch(
                EndpointFetchRequest(url=URL, etag="v1", last_modified="yesterday"), fetcher
            )
    return result, observed


def test_rss_valid_maps_ten_items_and_forwards_fetch_metadata():
    result, requests = parsed_fixture("rss_valid.xml")
    assert len(result.items) == 10
    assert result.rejected == ()
    assert [item.external_id for item in result.items] == [f"story-{n}" for n in range(1, 11)]
    assert [item.url for item in result.items] == [
        f"https://news.example/story/{n}" for n in range(1, 11)
    ]
    assert result.items[0].title == "Story 1"
    assert result.items[0].summary_html == "<p>Summary 1</p>"
    assert result.items[0].content_html == "<p>Body 1</p>"
    assert result.items[0].published_at == datetime(2015, 10, 21, 7, 28, tzinfo=timezone.utc)
    assert all(item.published_at.utcoffset().total_seconds() == 0 for item in result.items)
    assert result.items[0].authors == ("Writer 1",)
    assert result.items[0].language == result.feed_language == "en-US"
    assert result.items[0].raw["tags"] == [
        {"term": "Politics", "scheme": "https://news.example/topics"}
    ]
    assert result.items[0].raw["enclosures"] == [
        {"href": "https://cdn.example/audio.mp3", "type": "audio/mpeg", "length": "123"}
    ]
    assert result.items[0].raw["source"] == {
        "title": "Original Publisher",
        "href": "https://news.example/original",
    }
    assert result.items[0].raw["guid"] == "story-1"
    assert result.items[0].raw["guidislink"] is False
    assert result.etag == "v2"
    assert requests[0].headers["accept"] == RSS_ACCEPT
    assert "json" not in requests[0].headers["accept"]
    assert requests[0].headers["if-none-match"] == "v1"
    assert requests[0].headers["if-modified-since"] == "yesterday"


def test_atom_prefers_alternate_link_and_entry_xml_language():
    result, _ = parsed_fixture("atom_valid.xml", content_type="application/atom+xml")
    assert len(result.items) == 2
    assert result.items[0].external_id == "urn:story:atom-1"
    assert result.items[0].url == "https://news.example/story/atom-1"
    assert result.items[0].language == "fr"
    assert result.items[1].language == result.feed_language == "pt-BR"
    assert result.items[0].summary_html == "<p>Résumé</p>"
    assert result.items[0].content_html == "<p>Body one</p>"
    assert result.items[0].authors == ("Author One",)
    assert result.items[1].published_at == result.items[1].updated_at


def test_rss_1_rdf_is_parsed_by_the_same_adapter():
    result, _ = parsed_fixture("rss_rdf_valid.xml", content_type="application/rdf+xml")
    assert len(result.items) == 1
    assert result.items[0].title == "RDF item"
    assert result.items[0].url == "https://news.example/rdf/1"
    assert result.items[0].published_at == datetime(2015, 10, 21, 7, 28, tzinfo=timezone.utc)


def test_duplicate_entries_remain_separate():
    result, _ = parsed_fixture("rss_duplicate_entry.xml")
    assert len(result.items) == 2
    assert [item.external_id for item in result.items] == ["same-guid", "same-guid"]


def test_missing_optional_fields_do_not_reject_entry():
    result, _ = parsed_fixture("rss_missing_optional.xml")
    assert len(result.items) == 1
    item = result.items[0]
    assert item.authors == ()
    assert item.published_at is None
    assert item.updated_at is None
    assert item.summary_html is None


def test_malformed_entry_is_rejected_individually_and_bad_date_is_marked():
    result, _ = parsed_fixture("rss_malformed_item.xml")
    assert len(result.items) == 2
    assert [(entry.position, entry.reason) for entry in result.rejected] == [
        (1, "MISSING_IDENTITY")
    ]
    assert result.items[1].external_id == "invalid-date"
    assert result.items[1].published_at is None
    assert result.items[1].raw["date_parse_error"] == "published"


def test_id_only_and_mailto_only_are_both_delivered():
    result, _ = parsed_fixture("rss_id_only_entry.xml")
    assert len(result.items) == 2
    assert result.rejected == ()
    assert result.items[0].external_id == "guid-only-1"
    assert result.items[0].url is None
    assert result.items[1].external_id is None
    assert result.items[1].url == "mailto:editor@news.example"


def test_empty_entry_is_rejected_without_rejecting_feed():
    body = b'<rss version="2.0"><channel><title>Feed</title><item><guid>x</guid></item></channel></rss>'

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            return FetchResponse(body=body, content_type="application/rss+xml")

    result = RssAdapter().fetch(EndpointFetchRequest(url=URL), FakeFetcher())
    assert result.items == ()
    assert [(entry.position, entry.reason) for entry in result.rejected] == [(0, "EMPTY_ENTRY")]


def test_first_html_content_wins_over_xhtml_and_plain_text():
    entry = {
        "content": [
            {"type": "text/plain", "value": "plain"},
            {"type": "application/xhtml+xml", "value": "<p>xhtml</p>"},
            {"type": "text/html", "value": "<p>html first</p>"},
            {"type": "text/html", "value": "<p>html second</p>"},
        ]
    }
    assert _content(entry) == "<p>html first</p>"
    assert _content({"content": [{"type": "xhtml", "value": "<p>xhtml</p>"}]}) == "<p>xhtml</p>"
    assert _content({"content": [{"type": "text", "value": "plain"}]}) == "plain"


def test_updated_date_fills_published_at_when_published_is_absent():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><id>urn:feed</id>
    <updated>2015-10-21T07:28:00Z</updated><entry><id>urn:item</id><title>Item</title>
    <updated>2015-10-20T06:00:00Z</updated></entry></feed>"""

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            return FetchResponse(body=body, content_type="application/atom+xml")

    result = RssAdapter().fetch(EndpointFetchRequest(url=URL), FakeFetcher())
    assert result.items[0].published_at == datetime(2015, 10, 20, 6, tzinfo=timezone.utc)
    assert result.items[0].updated_at == result.items[0].published_at


def test_iso_8859_1_bytes_decode_using_response_charset():
    body = fixture("rss_encoding_iso88591.xml")
    assert b"Caf\xe9" in body
    result, _ = parsed_fixture(
        "rss_encoding_iso88591.xml", content_type="application/rss+xml; charset=iso-8859-1"
    )
    assert result.items[0].title == "Café e açúcar"


def test_html_content_preserves_suppressed_element_boundaries_for_normalization():
    result, _ = parsed_fixture("rss_html_content.xml")
    html = result.items[0].content_html
    assert "<p>" in html and "<strong>" in html
    assert "<script" in html and "<style" in html and "<iframe" in html


def test_wrong_media_type_fails_in_fetcher_without_parsing(monkeypatch):
    def no_parse(*args, **kwargs):
        raise AssertionError("parser must not run")

    monkeypatch.setattr("news.adapters.rss.feedparser.parse", no_parse)
    with pytest.raises(FetchError) as error:
        parsed_fixture("not_a_feed.html", content_type="text/html")
    assert error.value.kind == FetchErrorKind.UNSUPPORTED_CONTENT


def test_html_labeled_as_rss_is_malformed():
    with pytest.raises(FetchError) as error:
        parsed_fixture("not_a_feed.html")
    assert error.value.kind == FetchErrorKind.MALFORMED
    assert not error.value.retryable
    assert "Not a feed" not in error.value.message


def test_304_skips_parser_and_preserves_validators(monkeypatch):
    def no_parse(*args, **kwargs):
        raise AssertionError("parser must not run")

    monkeypatch.setattr("news.adapters.rss.feedparser.parse", no_parse)

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            assert (url, etag, last_modified) == (URL, "v1", "yesterday")
            return FetchResponse(not_modified=True, etag="v2", last_modified="today")

    result = RssAdapter().fetch(
        EndpointFetchRequest(url=URL, etag="v1", last_modified="yesterday"), FakeFetcher()
    )
    assert result.not_modified
    assert result.items == result.rejected == ()
    assert (result.etag, result.last_modified) == ("v2", "today")


def test_bozo_with_entries_warns_without_exposing_payload(pulso_caplog):
    body = fixture("rss_valid.xml").replace(b"</channel></rss>", b"</channel>")

    class FakeFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            return FetchResponse(body=body, content_type="application/rss+xml")

    result = RssAdapter().fetch(EndpointFetchRequest(url=URL), FakeFetcher())
    assert len(result.items) == 10
    assert "adapter=RSS entries=10" in pulso_caplog.text
    assert "Body 1" not in pulso_caplog.text


def test_raw_is_json_safe_and_bounded_for_all_fixtures():
    names = (
        "rss_valid.xml",
        "atom_valid.xml",
        "rss_duplicate_entry.xml",
        "rss_missing_optional.xml",
        "rss_malformed_item.xml",
        "rss_id_only_entry.xml",
        "rss_html_content.xml",
        "rss_encoding_iso88591.xml",
        "rss_rdf_valid.xml",
    )
    for name in names:
        content_type = (
            "application/rss+xml; charset=iso-8859-1" if "encoding" in name else "text/xml"
        )
        result, _ = parsed_fixture(name, content_type=content_type)
        for item in result.items:
            encoded = json.dumps(item.raw).encode("utf-8")
            assert len(encoded) < 16 * 1024
            assert "content" not in item.raw
            assert "summary" not in item.raw


def test_raw_metadata_bounds_long_and_repeated_values():
    entry = {
        "tags": [{"term": "x" * 5000, "unknown": "secret"}] * 100,
        "enclosures": [{"href": "https://cdn.example/" + "x" * 5000, "binary": b"secret"}] * 100,
        "source": {"title": "x" * 5000, "unexpected": "secret"},
        "guid": "x" * 5000,
    }
    raw = _raw(entry, [])
    assert len(raw["tags"]) == len(raw["enclosures"]) == 8
    assert len(raw["tags"][0]["term"]) == 256
    assert "unknown" not in raw["tags"][0]
    assert "binary" not in raw["enclosures"][0]
    assert len(json.dumps(raw).encode("utf-8")) < 16 * 1024


def test_adapter_obeys_source_adapter_protocol_and_preserves_fetch_errors():
    assert isinstance(RssAdapter(), SourceAdapter)

    class FailingFetcher:
        def get(self, url, *, accept, etag=None, last_modified=None):
            raise FetchError(FetchErrorKind.TIMEOUT, "Upstream request timed out", retryable=True)

    with pytest.raises(FetchError) as error:
        RssAdapter().fetch(EndpointFetchRequest(url=URL), FailingFetcher())
    assert error.value.kind == FetchErrorKind.TIMEOUT
    assert error.value.retryable
