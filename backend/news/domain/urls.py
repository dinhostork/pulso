"""Conservative URL identity rules for publications.

Rule table:
- lowercase the scheme and IDNA-encoded host; remove only :80 for HTTP and
  :443 for HTTPS;
- turn an empty path into '/', preserve other paths and trailing slashes;
- uppercase existing percent-escape hex digits without decoding them;
- drop fragments and the PR-maintained tracking-parameter allowlist;
- sort remaining query items by key, preserving repeated keys, blanks, and
  their original raw values (including '+' versus '%20').

Do not upgrade HTTP, strip www, resolve dot segments, decode reserved escapes,
or collapse duplicate parameters: those changes may merge distinct publications.
This module validates URL syntax only; fetch-target policy belongs to #13.
"""

import ipaddress
import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit


class InvalidUrl(ValueError):
    """A URL cannot be used as a publication identity."""


# Maintained explicitly in PRs; adding keys changes publication identity.
TRACKING_PARAMETERS = frozenset(
    {
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
)

_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


def _normalize_escapes(value: str) -> str:
    if _BAD_ESCAPE.search(value):
        raise InvalidUrl("Malformed percent escape")
    return _PERCENT_ESCAPE.sub(lambda match: f"%{match.group(1).upper()}", value)


def _hostname(host: str) -> str:
    if ":" in host:
        try:
            return f"[{ipaddress.IPv6Address(host).compressed}]"
        except ipaddress.AddressValueError as error:
            raise InvalidUrl("Invalid IPv6 host") from error
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise InvalidUrl("Invalid hostname") from error
    labels = ascii_host.removesuffix(".").split(".")
    if not labels or any(len(label) > 63 or not _HOST_LABEL.fullmatch(label) for label in labels):
        raise InvalidUrl("Invalid hostname")
    if len(ascii_host) > 253:
        raise InvalidUrl("Invalid hostname")
    return ascii_host


def _query_without_tracking(query: str) -> str:
    if not query:
        return ""
    _normalize_escapes(query)
    retained = []
    for raw_item in query.split("&"):
        raw_key = raw_item.partition("=")[0]
        key = unquote_plus(raw_key)
        folded_key = key.casefold()
        if folded_key.startswith("utm_") or folded_key in TRACKING_PARAMETERS:
            continue
        retained.append((key, _normalize_escapes(raw_item)))
    # Python's stable sort keeps repeated keys in their original order.
    retained.sort(key=lambda item: item[0])
    return "&".join(item for _, item in retained)


def canonicalize_url(url: str) -> str:
    """Return a conservative HTTP(S) publication URL or raise InvalidUrl.

    Surrounding whitespace is stripped. Embedded whitespace, controls,
    backslashes, malformed percent escapes, credentials, and malformed hosts
    or ports are rejected rather than repaired.
    """

    if not isinstance(url, str):
        raise InvalidUrl("URL must be a string")
    value = url.strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
        raise InvalidUrl("URL contains whitespace or control characters")
    if "\\" in value:
        raise InvalidUrl("URL contains a backslash")
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.netloc or not parts.hostname:
            raise InvalidUrl("URL requires an HTTP(S) scheme and hostname")
        if parts.username is not None or parts.password is not None:
            raise InvalidUrl("URL credentials are not supported")
        host = _hostname(parts.hostname)
        if parts.netloc.endswith(":"):
            raise InvalidUrl("Invalid port")
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise InvalidUrl("Invalid port")
    except ValueError as error:
        if isinstance(error, InvalidUrl):
            raise
        raise InvalidUrl("Malformed URL or port") from error

    default_port = 80 if scheme == "http" else 443
    authority = host if port is None or port == default_port else f"{host}:{port}"
    path = _normalize_escapes(parts.path or "/")
    query = _query_without_tracking(parts.query)
    return urlunsplit((scheme, authority, path, query, ""))
