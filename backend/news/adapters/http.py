"""Bounded HTTP transport shared by future RSS and JSON Feed adapters."""

import zlib
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings

from news.application.ports import FetchError, FetchErrorKind, FetchResponse

from .targets import assert_allowed_target

USER_AGENT = "PulsoBot/0.2 (+https://github.com/dinhostork/pulso)"
DEFAULT_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml, "
    "application/xml, text/xml, text/plain, application/feed+json, application/json"
)
ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/xml",
        "text/xml",
        "text/plain",
        "application/feed+json",
        "application/json",
    }
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _retry_after(value: str | None) -> int | None:
    """Parse integer Retry-After seconds; malformed values leave scheduling to caller."""

    if value is None or not value.strip().isascii() or not value.strip().isdigit():
        return None
    digits = value.strip().lstrip("0") or "0"
    return 900 if len(digits) > 3 else min(int(digits), 900)


def _exceeds_limit(value: str, limit: int) -> bool:
    """Compare decimal header digits without converting an unbounded integer."""

    digits = value.lstrip("0") or "0"
    bound = str(limit)
    return len(digits) > len(bound) or len(digits) == len(bound) and digits > bound


def _bounded_body(response: httpx.Response, cap: int, *, has_length: bool) -> bytes:
    """Limit decoded output before a compressed chunk can expand in memory."""

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in {"", "identity"}:
        decoder = None
    elif encoding == "gzip":
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise FetchError(
            FetchErrorKind.UNSUPPORTED_CONTENT, "Upstream content encoding is unsupported"
        )

    # A custom transport may return an already-buffered response. httpx has
    # decoded its content in that case; real network streams take the raw path.
    already_decoded = response.is_stream_consumed
    chunks = (response.content,) if already_decoded else response.iter_raw(chunk_size=64 * 1024)
    if already_decoded:
        decoder = None
    # Content-Length measures encoded bytes, not the size after decompression.
    known_decoded_length = has_length and encoding in {"", "identity"}
    body = bytearray()
    try:
        for raw_chunk in chunks:
            remaining = cap - len(body)
            chunk = decoder.decompress(raw_chunk, remaining + 1) if decoder else raw_chunk
            if len(chunk) > remaining:
                raise FetchError(FetchErrorKind.TOO_LARGE, "Response exceeds size limit")
            body.extend(chunk)
            if decoder and decoder.unconsumed_tail:
                raise FetchError(FetchErrorKind.TOO_LARGE, "Response exceeds size limit")
            if not known_decoded_length and len(body) == cap:
                raise FetchError(FetchErrorKind.TOO_LARGE, "Response reaches size limit")
        if decoder:
            remaining = cap - len(body)
            tail = decoder.flush(remaining + 1)
            if len(tail) > remaining:
                raise FetchError(FetchErrorKind.TOO_LARGE, "Response exceeds size limit")
            body.extend(tail)
            if not decoder.eof:
                raise FetchError(FetchErrorKind.MALFORMED, "Compressed response is incomplete")
            if len(body) == cap:
                raise FetchError(FetchErrorKind.TOO_LARGE, "Response reaches size limit")
    except zlib.error as error:
        raise FetchError(FetchErrorKind.MALFORMED, "Compressed response is malformed") from error
    return bytes(body)


class Fetcher:
    """One client per run, with target validation before every network hop.

    The target policy validates DNS first, but httpx resolves again at connect
    time. DNS rebinding in that gap remains possible until IP pinning is added.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: Callable[[str, int], Iterable[str]] | None = None,
        allow_private: bool | None = None,
    ):
        self._timeout = httpx.Timeout(
            connect=settings.NEWS_FETCH_CONNECT_TIMEOUT_SECONDS,
            read=settings.NEWS_FETCH_READ_TIMEOUT_SECONDS,
            write=settings.NEWS_FETCH_WRITE_TIMEOUT_SECONDS,
            pool=settings.NEWS_FETCH_POOL_TIMEOUT_SECONDS,
        )
        self._client = (
            client
            if client is not None
            else httpx.Client(timeout=self._timeout, follow_redirects=False, trust_env=False)
        )
        self._owns_client = client is None
        self._resolver = resolver
        self._allow_private = (
            settings.NEWS_FETCH_ALLOW_PRIVATE_NETWORKS if allow_private is None else allow_private
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._owns_client:
            self._client.close()

    def get(
        self,
        url: str,
        *,
        accept: str = DEFAULT_ACCEPT,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse:
        """Fetch a bounded HTTP(S) body without parsing its source format."""

        current = url
        for redirect_count in range(settings.NEWS_FETCH_MAX_REDIRECTS + 1):
            assert_allowed_target(
                current, allow_private=self._allow_private, resolver=self._resolver
            )
            headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "identity"}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            try:
                with self._client.stream(
                    "GET", current, headers=headers, timeout=self._timeout, follow_redirects=False
                ) as response:
                    status = response.status_code
                    if status in REDIRECT_STATUSES:
                        if redirect_count >= settings.NEWS_FETCH_MAX_REDIRECTS:
                            raise FetchError(
                                FetchErrorKind.TOO_MANY_REDIRECTS,
                                "Redirect limit exceeded",
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(FetchErrorKind.MALFORMED, "Redirect has no location")
                        try:
                            next_url = urljoin(current, location)
                            next_scheme = urlsplit(next_url).scheme.lower()
                        except ValueError as error:
                            raise FetchError(
                                FetchErrorKind.MALFORMED, "Redirect location is malformed"
                            ) from error
                        if urlsplit(current).scheme.lower() == "https" and next_scheme == "http":
                            raise FetchError(
                                FetchErrorKind.BLOCKED_TARGET,
                                "HTTPS downgrade is blocked",
                            )
                        # A validator belongs to its origin; never forward it across hosts.
                        if urlsplit(current).netloc.lower() != urlsplit(next_url).netloc.lower():
                            etag = None
                            last_modified = None
                        current = next_url
                        continue
                    if status == 304:
                        return FetchResponse(
                            not_modified=True,
                            etag=response.headers.get("etag", etag),
                            last_modified=response.headers.get("last-modified", last_modified),
                        )
                    if status == 429:
                        raise FetchError(
                            FetchErrorKind.RATE_LIMITED,
                            "Upstream rate limited the request",
                            retryable=True,
                            http_status=status,
                            retry_after=_retry_after(response.headers.get("retry-after")),
                        )
                    if not 200 <= status < 300:
                        raise FetchError(
                            FetchErrorKind.HTTP_STATUS,
                            "Upstream returned an unsuccessful status",
                            retryable=status == 408 or 500 <= status < 600,
                            http_status=status,
                        )

                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    media_type = media_type.strip().lower()
                    if media_type not in ALLOWED_MEDIA_TYPES and status != 204:
                        raise FetchError(
                            FetchErrorKind.UNSUPPORTED_CONTENT,
                            "Upstream content type is unsupported",
                            http_status=status,
                        )
                    cap = settings.NEWS_FETCH_MAX_RESPONSE_BYTES
                    length = response.headers.get("content-length", "")
                    has_length = length.isascii() and length.isdigit()
                    if has_length and _exceeds_limit(length, cap):
                        raise FetchError(FetchErrorKind.TOO_LARGE, "Response exceeds size limit")
                    body = _bounded_body(response, cap, has_length=has_length)
                    return FetchResponse(
                        body=body,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        content_type=response.headers.get("content-type"),
                    )
            except httpx.TimeoutException as error:
                raise FetchError(
                    FetchErrorKind.TIMEOUT, "Upstream request timed out", retryable=True
                ) from error
            except httpx.RequestError as error:
                raise FetchError(
                    FetchErrorKind.NETWORK, "Upstream network request failed", retryable=True
                ) from error

        raise FetchError(FetchErrorKind.TOO_MANY_REDIRECTS, "Redirect limit exceeded")
