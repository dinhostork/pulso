"""Publication identity precedence without database or network access."""

import pytest

from news.domain.identity import ExternalKey, ExternalKeyKind, MissingIdentity, external_key


@pytest.mark.parametrize(
    ("external_id", "url", "expected"),
    [
        (
            "guid-1",
            "https://Example.com/a?utm_source=x",
            ExternalKey(ExternalKeyKind.EXTERNAL_ID, "guid-1"),
        ),
        ("guid-1", None, ExternalKey(ExternalKeyKind.EXTERNAL_ID, "guid-1")),
        (
            None,
            "https://Example.com/a?utm_source=x",
            ExternalKey(ExternalKeyKind.CANONICAL_URL, "https://example.com/a"),
        ),
        (None, None, MissingIdentity()),
        ("", "mailto:x@y", MissingIdentity()),
        (None, "not a url", MissingIdentity()),
        ("   ", None, MissingIdentity()),
        (
            "  ",
            "https://example.com/x",
            ExternalKey(ExternalKeyKind.CANONICAL_URL, "https://example.com/x"),
        ),
        (" provider-id ", None, ExternalKey(ExternalKeyKind.EXTERNAL_ID, " provider-id ")),
    ],
)
def test_external_key_vectors(external_id, url, expected):
    assert external_key(external_id, url) == expected


def test_external_key_kind_values_match_news_persistence_contract():
    assert ExternalKeyKind.EXTERNAL_ID == "EXTERNAL_ID"
    assert ExternalKeyKind.CANONICAL_URL == "CANONICAL_URL"
