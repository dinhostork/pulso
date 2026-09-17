"""Infrastructure-independent contracts between News use cases and adapters."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class FetchedItem:
    """One provider item before normalization; raw holds JSON-safe source details."""

    external_id: str | None
    url: str | None
    title: str | None
    summary_html: str | None = None
    content_html: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    authors: tuple[str, ...] = ()
    language: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectedItem:
    """An unmappable entry with a bounded, operator-safe explanation."""

    position: int
    reason: str
    detail: str = ""

    def __post_init__(self):
        # Adapters must use safe summaries, never source payloads or headers.
        clean = " ".join(self.detail.split())[:200]
        object.__setattr__(self, "detail", clean)


@dataclass(frozen=True)
class FetchResult:
    """Parsed endpoint result returned by a format adapter."""

    items: tuple[FetchedItem, ...] = ()
    rejected: tuple[RejectedItem, ...] = ()
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    feed_language: str | None = None


@dataclass(frozen=True)
class EndpointFetchRequest:
    """Application-owned input; no Django model crosses this boundary."""

    url: str
    adapter_config: Mapping[str, object] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None


class FetchErrorKind(StrEnum):
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    HTTP_STATUS = "HTTP_STATUS"
    RATE_LIMITED = "RATE_LIMITED"
    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    MALFORMED = "MALFORMED"
    BLOCKED_TARGET = "BLOCKED_TARGET"
    TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"


class FetchError(Exception):
    """Safe operational failure metadata; no response or request object is held."""

    def __init__(
        self,
        kind: FetchErrorKind,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after: int | None = None,
    ):
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after = retry_after
        super().__init__(message)


@dataclass(frozen=True)
class FetchResponse:
    """Bounded transport response exposed to a format adapter."""

    body: bytes = b""
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None

    @property
    def response_headers(self) -> Mapping[str, str]:
        """Only parser-relevant metadata, never cookies or arbitrary headers."""

        return {"content-type": self.content_type} if self.content_type else {}


class FetcherPort(Protocol):
    """Minimal outbound transport used by source-format adapters."""

    def get(
        self,
        url: str,
        *,
        accept: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse: ...


@runtime_checkable
class SourceAdapter(Protocol):
    kind: ClassVar[str]

    def fetch(self, request: EndpointFetchRequest, fetcher: FetcherPort) -> FetchResult: ...
