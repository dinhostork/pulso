"""MockTransport exercises outbound policy without live DNS or HTTP."""

import gzip
import tracemalloc
import zlib

import httpx
import pytest
from django.conf import settings

from news.adapters.http import USER_AGENT, Fetcher
from news.application.ports import FetchError, FetchErrorKind


def fetch(handler, *, url="https://feed.example/rss", resolver=None, **kwargs):
    resolver = resolver or (lambda _host, _port: ("8.8.8.8",))
    kwargs.setdefault("allow_private", False)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with Fetcher(client=client, resolver=resolver, **kwargs) as fetcher:
            return fetcher.get(url)


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


def test_success_and_parser_metadata():
    result = fetch(
        lambda _request: httpx.Response(
            200,
            content=b"<rss />",
            headers={
                "Content-Type": "application/rss+xml; charset=utf-8",
                "ETag": "v1",
                "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
                "Set-Cookie": "SECRET_COOKIE",
            },
        )
    )
    assert result.body == b"<rss />"
    assert result.content_type == "application/rss+xml; charset=utf-8"
    assert result.response_headers == {"content-type": result.content_type}
    assert result.etag == "v1"
    assert result.last_modified == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert not result.not_modified


@pytest.mark.parametrize(
    "media_type",
    [
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/xml",
        "text/xml",
        "text/plain",
        "application/feed+json",
        "application/json",
    ],
)
def test_approved_media_types(media_type):
    result = fetch(
        lambda _request: httpx.Response(
            200, content=b"feed", headers={"Content-Type": media_type.upper() + "; charset=utf-8"}
        )
    )
    assert result.body == b"feed"


def test_html_is_rejected_without_body_or_header_leakage():
    secret = "SECRET_VALUE_DO_NOT_LEAK"
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200,
                content=secret.encode(),
                headers={"Content-Type": "text/html", "Set-Cookie": secret},
            )
        )
    assert error.value.kind == FetchErrorKind.UNSUPPORTED_CONTENT
    assert not error.value.retryable
    assert secret not in error.value.message


def test_content_length_precheck_does_not_read_body():
    stream = CountingStream([b"should not be read"])
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/rss+xml", "Content-Length": "6000000"},
                stream=stream,
            )
        )
    assert error.value.kind == FetchErrorKind.TOO_LARGE
    assert stream.read_count == 0


def test_oversized_content_length_digits_are_rejected_without_integer_conversion():
    stream = CountingStream([b"should not be read"])
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "text/xml", "Content-Length": "9" * 5000},
                stream=stream,
            )
        )
    assert error.value.kind == FetchErrorKind.TOO_LARGE
    assert stream.read_count == 0


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress)],
)
def test_compressed_feed_is_decoded_within_limit(encoding, compress):
    content = b"<rss>feed</rss>"
    compressed = compress(content)
    result = fetch(
        lambda _request: httpx.Response(
            200,
            headers={
                "Content-Type": "application/rss+xml",
                "Content-Encoding": encoding,
                "Content-Length": str(len(compressed)),
            },
            stream=httpx.ByteStream(compressed),
        )
    )
    assert result.body == content


def test_compressed_bomb_is_rejected_before_large_allocation():
    compressed = gzip.compress(b"x" * (30 * 1024 * 1024))
    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as error:
            fetch(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "text/xml",
                        "Content-Encoding": "gzip",
                        "Content-Length": str(len(compressed)),
                    },
                    stream=httpx.ByteStream(compressed),
                )
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.kind == FetchErrorKind.TOO_LARGE
    assert peak < 15 * 1024 * 1024


def test_chunked_body_aborts_near_cap_without_reading_entire_stream():
    chunks = [b"x" * (64 * 1024)] * 100
    stream = CountingStream(chunks)
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "application/rss+xml"}, stream=stream
            )
        )
    assert error.value.kind == FetchErrorKind.TOO_LARGE
    assert stream.read_count == settings.NEWS_FETCH_MAX_RESPONSE_BYTES // (64 * 1024)
    assert stream.read_count < len(chunks)


def test_unknown_length_at_exact_cap_is_refused_conservatively():
    stream = CountingStream([b"x" * (64 * 1024)] * 80)
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "application/rss+xml"}, stream=stream
            )
        )
    assert error.value.kind == FetchErrorKind.TOO_LARGE
    assert stream.read_count == 80


def test_exact_cap_is_accepted():
    content = b"x" * settings.NEWS_FETCH_MAX_RESPONSE_BYTES
    result = fetch(
        lambda _request: httpx.Response(
            200, content=content, headers={"Content-Type": "application/rss+xml"}
        )
    )
    assert len(result.body) == settings.NEWS_FETCH_MAX_RESPONSE_BYTES


def test_304_keeps_validators_and_does_not_read_body():
    stream = CountingStream([b"should not be read"])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(304, headers={"ETag": "v2"}, stream=stream)
        )
    ) as client:
        with Fetcher(client=client, resolver=lambda _host, _port: ("8.8.8.8",)) as fetcher:
            result = fetcher.get("https://feed.example/rss", etag="v1", last_modified="old")
    assert result.not_modified
    assert result.body == b""
    assert result.etag == "v2"
    assert result.last_modified == "old"
    assert stream.read_count == 0


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (408, FetchErrorKind.HTTP_STATUS, True),
        (404, FetchErrorKind.HTTP_STATUS, False),
        (503, FetchErrorKind.HTTP_STATUS, True),
        (429, FetchErrorKind.RATE_LIMITED, True),
    ],
)
def test_status_classification(status, kind, retryable):
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: httpx.Response(status, content=b"SECRET_BODY"))
    assert error.value.kind == kind
    assert error.value.retryable == retryable
    assert error.value.http_status == status
    assert "SECRET_BODY" not in error.value.message


@pytest.mark.parametrize(
    ("header", "expected"),
    [("120", 120), ("99999", 900), ("invalid", None), ("9" * 5000, 900)],
)
def test_retry_after_is_capped_and_malformed_value_is_safe(header, expected):
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: httpx.Response(429, headers={"Retry-After": header}))
    assert error.value.retry_after == expected


def test_non_ascii_digit_retry_after_uses_safe_fallback():
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: httpx.Response(429, headers={b"Retry-After": b"\xb2"}))
    assert error.value.kind == FetchErrorKind.RATE_LIMITED
    assert error.value.retry_after is None


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout("SECRET"), httpx.ReadTimeout("SECRET")])
def test_timeouts_are_retryable_and_sanitized(exception):
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: (_ for _ in ()).throw(exception))
    assert error.value.kind == FetchErrorKind.TIMEOUT
    assert error.value.retryable
    assert "SECRET" not in error.value.message


def test_connection_failure_is_retryable_and_sanitized():
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("SECRET")))
    assert error.value.kind == FetchErrorKind.NETWORK
    assert error.value.retryable
    assert "SECRET" not in error.value.message


def test_relative_redirect_is_followed_and_each_target_is_resolved():
    seen = []
    resolved = []

    def handler(request):
        seen.append(str(request.url))
        assert request.headers["user-agent"] == USER_AGENT
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new"})
        return httpx.Response(200, content=b"feed", headers={"Content-Type": "text/xml"})

    result = fetch(
        handler,
        url="https://feed.example/old",
        resolver=lambda host, port: resolved.append((host, port)) or ("8.8.8.8",),
    )
    assert result.body == b"feed"
    assert seen == ["https://feed.example/old", "https://feed.example/new"]
    assert resolved == [("feed.example", 443), ("feed.example", 443)]


def test_cross_origin_redirect_does_not_forward_endpoint_validators():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "feed.example":
            return httpx.Response(302, headers={"Location": "https://other.example/feed"})
        return httpx.Response(200, content=b"feed", headers={"Content-Type": "text/xml"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with Fetcher(client=client, resolver=lambda _host, _port: ("8.8.8.8",)) as fetcher:
            fetcher.get("https://feed.example/feed", etag="PRIVATE_ETAG", last_modified="DATE")
    assert seen[0].headers["if-none-match"] == "PRIVATE_ETAG"
    assert "if-none-match" not in seen[1].headers
    assert "if-modified-since" not in seen[1].headers


def test_redirect_to_private_target_is_blocked_before_connect():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})

    with pytest.raises(FetchError) as error:
        fetch(handler)
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET
    assert requested == ["https://feed.example/rss"]


def test_sixth_redirect_is_refused():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        index = int(request.url.path.removeprefix("/"))
        return httpx.Response(302, headers={"Location": f"/{index + 1}"})

    with pytest.raises(FetchError) as error:
        fetch(handler, url="https://feed.example/0")
    assert error.value.kind == FetchErrorKind.TOO_MANY_REDIRECTS
    assert not error.value.retryable
    assert len(requested) == 6


def test_https_to_http_downgrade_is_refused():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://feed.example/x"})

    with pytest.raises(FetchError) as error:
        fetch(handler)
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET
    assert requested == ["https://feed.example/rss"]


def test_missing_redirect_location_is_malformed():
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: httpx.Response(302))
    assert error.value.kind == FetchErrorKind.MALFORMED


def test_malformed_redirect_location_is_safely_classified():
    with pytest.raises(FetchError) as error:
        fetch(lambda _request: httpx.Response(302, headers={"Location": "http://[broken"}))
    assert error.value.kind == FetchErrorKind.MALFORMED


def test_conditional_headers_user_agent_and_adapter_accept():
    observed = {}

    def handler(request):
        observed.update(request.headers)
        return httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with Fetcher(client=client, resolver=lambda _host, _port: ("8.8.8.8",)) as fetcher:
            fetcher.get(
                "https://feed.example/data",
                accept="application/feed+json",
                etag='"abc"',
                last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
            )
    assert observed["user-agent"] == USER_AGENT
    assert observed["accept"] == "application/feed+json"
    assert observed["accept-encoding"] == "identity"
    assert observed["if-none-match"] == '"abc"'
    assert observed["if-modified-since"] == "Wed, 21 Oct 2015 07:28:00 GMT"


def test_private_override_does_not_disable_media_type_policy():
    with pytest.raises(FetchError) as error:
        fetch(
            lambda _request: httpx.Response(
                200, content=b"<html />", headers={"Content-Type": "text/html"}
            ),
            url="http://127.0.0.1/rss",
            allow_private=True,
        )
    assert error.value.kind == FetchErrorKind.UNSUPPORTED_CONTENT
