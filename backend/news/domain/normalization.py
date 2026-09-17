"""Pure normalization of persisted source payloads.

Rule table: title is NFKC/collapsed/required/max 512; description and body are
plain text, with body over 200,000 rejected; URL uses the existing canonicalizer;
byline joins usable authors up to 512; timestamps become aware UTC; language is
entry then Source default, lowercase/max 16; fingerprints use existing helpers.
Rejections are EMPTY_ENTRY, MISSING_TITLE, BODY_TOO_LARGE and MISSING_CANONICAL_URL.
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .fingerprints import content_fingerprint, fingerprint_input_length
from .html_text import html_to_text
from .urls import InvalidUrl, canonicalize_url

MAX_TITLE_LENGTH = 512
MAX_BYLINE_LENGTH = 512
MAX_BODY_LENGTH = 200_000


@dataclass(frozen=True)
class NormalizedArticle:
    external_id: str
    canonical_url: str
    title: str
    description: str
    body_text: str
    byline: str
    published_at: datetime | None
    language: str
    content_fingerprint: str
    fingerprint_input_length: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    reason: str


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _html(value: object) -> str:
    return html_to_text(value) if isinstance(value, str) else ""


def _published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def normalize(payload: object, *, source_default_language: object) -> NormalizedArticle | Rejection:
    """Normalize one immutable intake payload without database access."""

    data: Mapping = payload if isinstance(payload, Mapping) else {}
    original_title = _text(data.get("title"))
    description = _html(data.get("summary_html"))
    body_text = _html(data.get("content_html"))
    if not original_title and not description and not body_text:
        return Rejection("EMPTY_ENTRY")
    if not original_title:
        return Rejection("MISSING_TITLE")
    if len(body_text) > MAX_BODY_LENGTH:
        return Rejection("BODY_TOO_LARGE")

    notes = ()
    title = original_title
    if len(title) > MAX_TITLE_LENGTH:
        title = title[:MAX_TITLE_LENGTH]
        notes = ("TITLE_TRUNCATED",)
    try:
        canonical_url = canonicalize_url(data.get("url"))
    except InvalidUrl:
        return Rejection("MISSING_CANONICAL_URL")

    authors = data.get("authors")
    normalized_authors = (
        [_text(author) for author in authors if isinstance(author, str)]
        if isinstance(authors, list | tuple)
        else []
    )
    byline = ", ".join(author for author in normalized_authors if author)[:MAX_BYLINE_LENGTH]
    language = (_text(data.get("language")) or _text(source_default_language)).lower()[:16]
    fingerprint = content_fingerprint(title, body_text, description)
    return NormalizedArticle(
        external_id=_text(data.get("external_id")),
        canonical_url=canonical_url,
        title=title,
        description=description,
        body_text=body_text,
        byline=byline,
        published_at=_published_at(data.get("published_at")),
        language=language,
        content_fingerprint=fingerprint,
        fingerprint_input_length=fingerprint_input_length(title, body_text, description),
        notes=notes,
    )
