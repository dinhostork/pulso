"""Signed, versioned keyset cursors for News product reads."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core import signing
from django.utils import timezone

CURSOR_VERSION = 1
CURSOR_MAX_AGE = timedelta(hours=24)
_CURSOR_SALT = "pulso.news.product-cursor.v1"


class CursorError(ValueError):
    """A safe cursor rejection carrying a product error code."""

    def __init__(self, code: str = "invalid_cursor"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Cursor:
    scope: str
    page_size: int
    watermark: tuple[str, str]
    last: tuple[str, str]
    context: str | None = None
    account_id: str | None = None


def encode_cursor(
    *,
    scope: str,
    page_size: int,
    watermark: tuple[str, str],
    last: tuple[str, str],
    context: str | None = None,
    account_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Encode only fixed, non-executable pagination fields."""

    current = now or timezone.now()
    payload = {
        "v": CURSOR_VERSION,
        "scope": scope,
        "size": page_size,
        "watermark": list(watermark),
        "last": list(last),
        "context": context,
        "account": account_id,
        "expires_at": int((current + CURSOR_MAX_AGE).timestamp()),
    }
    return signing.dumps(payload, salt=_CURSOR_SALT, compress=True)


def _pair(value: Any) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise CursorError()
    return value[0], value[1]


def decode_cursor(
    token: str,
    *,
    expected_scope: str,
    page_size: int,
    expected_account_id: str | None = None,
    now: datetime | None = None,
) -> Cursor:
    """Verify signature, version, scope, size, binding and explicit expiry."""

    if not isinstance(token, str) or not token or len(token) > 4096:
        raise CursorError()
    try:
        payload = signing.loads(token, salt=_CURSOR_SALT)
    except signing.BadSignature:
        raise CursorError() from None
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "scope",
        "size",
        "watermark",
        "last",
        "context",
        "account",
        "expires_at",
    }:
        raise CursorError()
    if payload["v"] != CURSOR_VERSION:
        raise CursorError("unsupported_cursor_version")
    if payload["scope"] != expected_scope:
        raise CursorError("cursor_scope_mismatch")
    if payload["size"] != page_size:
        raise CursorError("cursor_page_size_mismatch")
    if payload["account"] != expected_account_id:
        raise CursorError("cursor_account_mismatch")
    if payload["context"] is not None and not isinstance(payload["context"], str):
        raise CursorError()
    expires_at = payload["expires_at"]
    if not isinstance(expires_at, int):
        raise CursorError()
    if expires_at <= int((now or timezone.now()).timestamp()):
        raise CursorError("cursor_expired")
    return Cursor(
        scope=payload["scope"],
        page_size=payload["size"],
        watermark=_pair(payload["watermark"]),
        last=_pair(payload["last"]),
        context=payload["context"],
        account_id=payload["account"],
    )
