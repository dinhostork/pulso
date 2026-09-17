"""Explicit environment parsing with value-free configuration errors."""

import os

from django.core.exceptions import ImproperlyConfigured


def required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ImproperlyConfigured(f"Missing required environment setting: {name}")
    return value


def boolean(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ImproperlyConfigured(f"{name} must be true or false")
    return normalized == "true"


def positive_int(name: str, *, default: int) -> int:
    """Parse a positive integer setting; an unset variable uses the default.

    An explicitly provided value must be a valid positive integer: zero,
    negative and non-integer values are configuration errors rather than
    silently corrected defaults.
    """

    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a positive integer") from None
    if parsed < 1:
        raise ImproperlyConfigured(f"{name} must be a positive integer")
    return parsed


def port(name: str) -> int:
    value = required(name)
    try:
        parsed = int(value)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer from 1 to 65535") from None
    if not 1 <= parsed <= 65535:
        raise ImproperlyConfigured(f"{name} must be an integer from 1 to 65535")
    return parsed
