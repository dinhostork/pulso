"""Deterministic hashes of received payloads and normalized publication text."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping


def payload_hash(payload: Mapping) -> str:
    """SHA-256 of UTF-8 JSON with sorted keys and no insignificant spaces.

    Unsupported JSON values and NaN/Infinity fail rather than acquiring an
    implicit or unstable string representation.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint_input(title: str, body_text: str, description: str) -> str:
    normalized_title = unicodedata.normalize("NFKC", title).casefold()
    content = body_text if body_text else description
    normalized_content = " ".join(content.split())
    return f"{normalized_title}\n{normalized_content}"


def content_fingerprint(title: str, body_text: str, description: str) -> str:
    """SHA-256 of NFKC/casefolded title, newline, and collapsed body or description.

    Body text takes precedence when nonempty. Content case and wording remain
    significant; this is an exact deterministic fingerprint, not similarity.
    """

    material = _fingerprint_input(title, body_text, description)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def fingerprint_input_length(title: str, body_text: str, description: str) -> int:
    """Count code points in precisely the normalized text hashed above, including newline."""

    return len(_fingerprint_input(title, body_text, description))
