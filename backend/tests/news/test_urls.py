"""Table-driven, network-free publication URL identity vectors."""

import ast
from pathlib import Path

import pytest

from news.domain.urls import TRACKING_PARAMETERS, InvalidUrl, canonicalize_url

VALID_URLS = [
    ("https://Example.com:443/a?utm_source=x&b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ("HTTPS://EXAMPLE.COM", "https://example.com/"),
    ("http://EXAMPLE.COM", "http://example.com/"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:443/x", "https://example.com/x"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("http://example.com/", "http://example.com/"),
    ("http://example.com/path/", "http://example.com/path/"),
    ("http://example.com/path", "http://example.com/path"),
    ("http://example.com/a/../b", "http://example.com/a/../b"),
    ("http://example.com/a//b", "http://example.com/a//b"),
    ("http://example.com/a%2fb", "http://example.com/a%2Fb"),
    ("http://example.com/a%2Fb", "http://example.com/a%2Fb"),
    ("http://example.com/a%20b", "http://example.com/a%20b"),
    ("https://example.com/article#section", "https://example.com/article"),
    ("https://example.com/article?b=2&a=1", "https://example.com/article?a=1&b=2"),
    ("https://example.com/x?ref=nav&id=7", "https://example.com/x?id=7&ref=nav"),
    (
        "https://example.com/x?page=2&article=3&lang=pt",
        "https://example.com/x?article=3&lang=pt&page=2",
    ),
    ("https://example.com/x?section=a&ref=b", "https://example.com/x?ref=b&section=a"),
    ("https://example.com/x?b=2&a=first&a=second", "https://example.com/x?a=first&a=second&b=2"),
    ("https://example.com/x?b=2&a=&a=3", "https://example.com/x?a=&a=3&b=2"),
    ("https://example.com/x?b=2&a", "https://example.com/x?a&b=2"),
    ("https://example.com/x?b=2&a=one+two", "https://example.com/x?a=one+two&b=2"),
    ("https://example.com/x?b=2&a=one%20two", "https://example.com/x?a=one%20two&b=2"),
    ("https://example.com/x?b=2&a=%2f", "https://example.com/x?a=%2F&b=2"),
    (
        "https://example.com/x?b=2&%61=first&a=second",
        "https://example.com/x?%61=first&a=second&b=2",
    ),
    ("https://example.com/x?utm_medium=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?UTM_CAMPAIGN=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?utm_%73ource=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?fbclid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?gclid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?dclid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?msclkid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?mc_cid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?mc_eid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?igshid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?yclid=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?_ga=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?_gl=x&id=7", "https://example.com/x?id=7"),
    ("https://example.com/x?ref_src=x&id=7", "https://example.com/x?id=7"),
    ("http://www.example.com/x", "http://www.example.com/x"),
    ("http://example.com/x", "http://example.com/x"),
    ("https://münich.example/x", "https://xn--mnich-kva.example/x"),
    ("https://XN--MNICH-KVA.EXAMPLE/x", "https://xn--mnich-kva.example/x"),
    ("  https://Example.com/x  ", "https://example.com/x"),
    ("http://127.0.0.1/x", "http://127.0.0.1/x"),
    ("http://[::1]/x", "http://[::1]/x"),
    ("https://example.com/?", "https://example.com/"),
]

INVALID_URLS = [
    "ftp://example.com/x",
    "javascript:alert(1)",
    "mailto:x@y",
    "file:///tmp/x",
    "example.com/x",
    "//example.com/x",
    "http:///x",
    "https://",
    "https://example.com/a b",
    "https://example.com/a\tb",
    "https://example.com/a\nb",
    "https://example.com/a\rb",
    "https://example.com/a\x00b",
    "https://example.com/a\x7fb",
    "https://example.com:abc/x",
    "https://example.com:99999/x",
    "https://example.com:0/x",
    "https://example.com:/x",
    "https://example.com/%q1",
    "https://example.com/x?a=%x1",
    "https://example.com/x?utm_source=%x1",
    "https://example.com\\@other.example/x",
    "https://user:pass@example.com/x",
    "https://bad..example/x",
]


@pytest.mark.parametrize(("url", "expected"), VALID_URLS)
def test_valid_url_vectors(url, expected):
    assert canonicalize_url(url) == expected
    assert canonicalize_url(expected) == expected


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_url_vectors(url):
    with pytest.raises(InvalidUrl):
        canonicalize_url(url)


def test_non_string_url_is_invalid():
    with pytest.raises(InvalidUrl):
        canonicalize_url(None)


def test_tracking_allowlist_is_exact():
    assert TRACKING_PARAMETERS == {
        "utm_*",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "yclid",
        "_ga",
        "_gl",
        "ref_src",
    }


def test_domain_modules_have_no_framework_or_network_imports():
    domain = Path(__file__).resolve().parents[2] / "news" / "domain"
    forbidden = {"django", "feedparser", "httpx", "news.models", "socket", "requests"}
    for filename in ("urls.py", "fingerprints.py", "identity.py"):
        tree = ast.parse((domain / filename).read_text())
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
