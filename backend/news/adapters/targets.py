"""Resolve and reject unsafe outbound News targets before every request.

This checks DNS answers before connecting, including redirects. The HTTP client
resolves again when it connects; without IP pinning, DNS rebinding between those
steps remains a residual risk.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from news.application.ports import FetchError, FetchErrorKind
from news.domain.urls import InvalidUrl, canonicalize_url

Resolver = Callable[[str, int], Iterable[str]]
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _resolve(host: str, port: int) -> tuple[str, ...]:
    return tuple(
        result[4][0]
        for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if result[0] in {socket.AF_INET, socket.AF_INET6}
    )


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return _is_blocked(address.ipv4_mapped)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
        or isinstance(address, ipaddress.IPv4Address)
        and address in _CGNAT
    )


def assert_allowed_target(
    url: str, *, allow_private: bool = False, resolver: Resolver | None = None
) -> None:
    """Reject a URL if any of its resolved destinations violates the policy.

    The private-network override skips only address-range refusal. Scheme and
    URL syntax remain mandatory, and callers still enforce redirects, timeouts,
    size and media-type limits.
    """

    try:
        if not isinstance(url, str) or url != url.strip():
            raise InvalidUrl("Invalid URL")
        canonicalize_url(url)  # Syntax only; never use its rewritten query for fetching.
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        if host is None:
            raise InvalidUrl("Missing hostname")
    except (InvalidUrl, ValueError) as error:
        raise FetchError(FetchErrorKind.BLOCKED_TARGET, "Invalid HTTP(S) target") from error

    try:
        # Literal addresses need no resolver; DNS hostnames check *all* answers.
        try:
            addresses = (ipaddress.ip_address(host),)
        except ValueError:
            resolved = tuple((resolver or _resolve)(host, port))
            addresses = tuple(ipaddress.ip_address(value) for value in resolved)
    except (OSError, ValueError) as error:
        raise FetchError(
            FetchErrorKind.NETWORK, "Target resolution failed", retryable=True
        ) from error
    if not addresses:
        raise FetchError(
            FetchErrorKind.NETWORK, "Target resolution returned no addresses", retryable=True
        )
    if not allow_private and any(_is_blocked(address) for address in addresses):
        raise FetchError(FetchErrorKind.BLOCKED_TARGET, "Target address is blocked")
