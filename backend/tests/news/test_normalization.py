from datetime import timezone
from pathlib import Path

import pytest

from news.adapters.jsonfeed import JsonFeedAdapter
from news.adapters.rss import RssAdapter
from news.application.ports import EndpointFetchRequest, FetchResponse
from news.domain.fingerprints import content_fingerprint, fingerprint_input_length
from news.domain.normalization import NormalizedArticle, Rejection, normalize

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"


def payload(**changes):
    value = {
        "external_id": " story-1 ",
        "url": "HTTPS://News.Example/item?b=2&utm_source=x&a=1#part",
        "title": "  Fullwidth Ａ   title ",
        "summary_html": "<p>A <b>summary</b>.</p>",
        "content_html": "<p>Body one.</p><p>Body two.</p>",
        "published_at": "2026-09-17T10:00:00-03:00",
        "authors": [" Author  One ", 42, "Ａuthor Two"],
        "language": "PT-BR",
    }
    value.update(changes)
    return value


def test_normalizes_all_publication_fields_and_existing_fingerprint_contract():
    result = normalize(payload(), source_default_language="en-US")
    assert isinstance(result, NormalizedArticle)
    assert result.external_id == "story-1"
    assert result.canonical_url == "https://news.example/item?a=1&b=2"
    assert result.title == "Fullwidth A title"
    assert result.description == "A summary."
    assert result.body_text == "Body one.\nBody two."
    assert result.byline == "Author One, Author Two"
    assert result.published_at.tzinfo is timezone.utc
    assert result.published_at.isoformat() == "2026-09-17T13:00:00+00:00"
    assert result.language == "pt-br"
    assert result.content_fingerprint == content_fingerprint(
        result.title, result.body_text, result.description
    )
    assert result.fingerprint_input_length == fingerprint_input_length(
        result.title, result.body_text, result.description
    )


def test_title_boundaries_truncation_note_and_title_only_entry():
    exact = normalize(
        payload(title="x" * 512, content_html=None, summary_html=None), source_default_language=""
    )
    long = normalize(payload(title="x" * 513), source_default_language="")
    assert isinstance(exact, NormalizedArticle) and len(exact.title) == 512 and exact.notes == ()
    assert isinstance(long, NormalizedArticle) and len(long.title) == 512
    assert long.notes == ("TITLE_TRUNCATED",)


def test_body_limit_accepts_boundary_and_rejects_overflow():
    exact = normalize(payload(content_html="x" * 200_000), source_default_language="")
    over = normalize(payload(content_html="x" * 200_001), source_default_language="")
    assert isinstance(exact, NormalizedArticle) and len(exact.body_text) == 200_000
    assert over == Rejection("BODY_TOO_LARGE")


@pytest.mark.parametrize("url", [None, "", "mailto:editor@example.com", {}, "https://bad host/"])
def test_missing_or_noncanonical_url_is_rejected(url):
    assert normalize(payload(url=url), source_default_language="") == Rejection(
        "MISSING_CANONICAL_URL"
    )


def test_timestamp_absence_invalid_and_naive_degrade_to_none():
    for value in (None, "invalid", "2026-09-17T10:00:00", {}):
        result = normalize(payload(published_at=value), source_default_language="")
        assert isinstance(result, NormalizedArticle) and result.published_at is None


def test_authors_byline_limit_language_fallback_and_empty_language():
    result = normalize(
        payload(authors=["a" * 300, "b" * 300], language=None),
        source_default_language="EN-us-LONGER-THAN-SIXTEEN",
    )
    assert isinstance(result, NormalizedArticle)
    assert 255 < len(result.byline) == 512
    assert result.language == "en-us-longer-tha"
    empty = normalize(payload(language=[]), source_default_language="")
    assert isinstance(empty, NormalizedArticle) and empty.language == ""


def test_empty_titleless_and_malformed_types_are_deterministic():
    assert normalize({}, source_default_language="") == Rejection("EMPTY_ENTRY")
    assert normalize(
        payload(title={}, summary_html=None, content_html=None), source_default_language=""
    ) == Rejection("EMPTY_ENTRY")
    assert normalize(
        payload(title=None, content_html="<p>Meaningful</p>"), source_default_language=""
    ) == Rejection("MISSING_TITLE")
    result = normalize(
        payload(authors="not-a-list", published_at={}, language=[], external_id={}),
        source_default_language={},
    )
    assert isinstance(result, NormalizedArticle)
    assert (result.external_id, result.byline, result.published_at, result.language) == (
        "",
        "",
        None,
        "",
    )


@pytest.mark.parametrize(
    ("adapter", "fixture_name", "content_type"),
    [
        *[
            (RssAdapter(), name, "application/rss+xml")
            for name in (
                "atom_valid.xml",
                "rss_changed_item_v1.xml",
                "rss_changed_item_v2.xml",
                "rss_duplicate_entry.xml",
                "rss_html_content.xml",
                "rss_id_only_entry.xml",
                "rss_malformed_item.xml",
                "rss_missing_optional.xml",
                "rss_rdf_valid.xml",
                "rss_tracking_urls.xml",
                "rss_valid.xml",
            )
        ],
        (JsonFeedAdapter(), "jsonfeed_valid.json", "application/feed+json"),
    ],
)
def test_every_accepted_fixture_item_normalizes_or_rejects_without_empty_url(
    adapter, fixture_name, content_type
):
    body = (FIXTURES / fixture_name).read_bytes()

    class FixturePort:
        def get(self, *_args, **_kwargs):
            return FetchResponse(body=body, content_type=content_type)

    result = adapter.fetch(EndpointFetchRequest(url="https://feed.example/source"), FixturePort())
    for item in result.items:
        normalized = normalize(
            {
                "external_id": item.external_id,
                "url": item.url,
                "title": item.title,
                "summary_html": item.summary_html,
                "content_html": item.content_html,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "authors": list(item.authors),
                "language": item.language,
            },
            source_default_language="",
        )
        if isinstance(normalized, NormalizedArticle):
            assert normalized.canonical_url
        else:
            assert normalized.reason in {"EMPTY_ENTRY", "MISSING_TITLE", "MISSING_CANONICAL_URL"}
