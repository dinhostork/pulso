"""Translate JSON Feed 1.x documents into the shared News adapter DTOs."""

import json
import math
import re
from datetime import datetime, timezone

from news.application.ports import (
    EndpointFetchRequest,
    FetchedItem,
    FetcherPort,
    FetchError,
    FetchErrorKind,
    FetchResult,
    RejectedItem,
)

JSON_ACCEPT = "application/feed+json, application/json"
_VERSION = re.compile(r"https://jsonfeed\.org/version/1(?:\.[0-9]+)?\Z")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
MAX_RAW_LIST_ITEMS = 8
MAX_RAW_TEXT = 256
MAX_RAW_NUMBER = 1_000_000_000_000


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _authors(item: dict) -> tuple[str, ...]:
    names = []
    if isinstance(item.get("authors"), list):
        for author in item["authors"]:
            if isinstance(author, dict) and (name := _text(author.get("name"))):
                names.append(name)
    if not names and isinstance(item.get("author"), dict):
        if name := _text(item["author"].get("name")):
            names.append(name)
    return tuple(names)


def _raw(item: dict, date_errors: list[str]) -> dict[str, object]:
    raw: dict[str, object] = {}
    if isinstance(item.get("tags"), list):
        tags = []
        for tag in item["tags"]:
            if value := _text(tag):
                tags.append(value[:MAX_RAW_TEXT])
                if len(tags) == MAX_RAW_LIST_ITEMS:
                    break
        if tags:
            raw["tags"] = tags[:MAX_RAW_LIST_ITEMS]
    if value := _text(item.get("external_url")):
        raw["external_url"] = value[:MAX_RAW_TEXT]
    if isinstance(item.get("attachments"), list):
        attachments = []
        for attachment in item["attachments"][:MAX_RAW_LIST_ITEMS]:
            if not isinstance(attachment, dict):
                continue
            selected: dict[str, str | int | float] = {}
            for key in ("url", "mime_type", "title"):
                if value := _text(attachment.get(key)):
                    selected[key] = value[:MAX_RAW_TEXT]
            for key in ("size_in_bytes", "duration_in_seconds"):
                number = attachment.get(key)
                if (
                    isinstance(number, int | float)
                    and not isinstance(number, bool)
                    and (not isinstance(number, float) or math.isfinite(number))
                    and 0 <= number <= MAX_RAW_NUMBER
                ):
                    selected[key] = number
            if selected:
                attachments.append(selected)
        if attachments:
            raw["attachments"] = attachments
    if date_errors:
        raw["date_parse_error"] = ",".join(date_errors)
    return raw


def _item(item: dict, feed_language: str | None) -> FetchedItem:
    published = _date(item.get("date_published"))
    modified = _date(item.get("date_modified"))
    date_errors = []
    if item.get("date_published") is not None and published is None:
        date_errors.append("date_published")
    if item.get("date_modified") is not None and modified is None:
        date_errors.append("date_modified")
    content = _text(item.get("content_html")) or _text(item.get("content_text"))
    return FetchedItem(
        external_id=_text(item.get("id")),
        url=_text(item.get("url")),
        title=_text(item.get("title")),
        summary_html=_text(item.get("summary")),
        content_html=content,
        published_at=published,
        updated_at=modified,
        authors=_authors(item),
        language=_text(item.get("language")) or feed_language,
        raw=_raw(item, date_errors),
    )


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class JsonFeedAdapter:
    """Map JSON Feed fields to the format-independent FetchedItem contract.

    FetchedItem field | JSON Feed item field
    external_id      | id
    url              | url
    title            | title
    summary_html     | summary
    content_html     | content_html, falling back to content_text
    published_at     | date_published (RFC 3339, UTC)
    updated_at       | date_modified (RFC 3339, UTC)
    authors          | authors[].name, falling back to author.name
    language         | item.language, then feed.language
    raw              | bounded tags, external_url, attachments and date marker
    """

    kind = "JSON_FEED"

    def fetch(self, request: EndpointFetchRequest, fetcher: FetcherPort) -> FetchResult:
        response = fetcher.get(
            request.url,
            accept=JSON_ACCEPT,
            etag=request.etag,
            last_modified=request.last_modified,
        )
        if response.not_modified:
            return FetchResult(
                not_modified=True, etag=response.etag, last_modified=response.last_modified
            )
        try:
            document = json.loads(response.body, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise FetchError(FetchErrorKind.MALFORMED, "JSON Feed document is malformed") from error
        if not isinstance(document, dict):
            raise FetchError(FetchErrorKind.MALFORMED, "JSON Feed root must be an object")
        version = document.get("version")
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise FetchError(FetchErrorKind.MALFORMED, "JSON Feed version is unsupported")
        entries = document.get("items")
        if not isinstance(entries, list):
            raise FetchError(FetchErrorKind.MALFORMED, "JSON Feed items must be an array")

        feed_language = _text(document.get("language"))
        items = []
        rejected = []
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict) or any(
                key in entry and entry[key] is not None and not isinstance(entry[key], str)
                for key in ("id", "url")
            ):
                rejected.append(RejectedItem(position=position, reason="MALFORMED_ENTRY"))
                continue
            item = _item(entry, feed_language)
            if not item.external_id and not item.url:
                rejected.append(RejectedItem(position=position, reason="MISSING_IDENTITY"))
            elif not any(
                (value or "").strip()
                for value in (item.title, item.summary_html, item.content_html)
            ):
                rejected.append(RejectedItem(position=position, reason="EMPTY_ENTRY"))
            else:
                items.append(item)
        return FetchResult(
            items=tuple(items),
            rejected=tuple(rejected),
            etag=response.etag,
            last_modified=response.last_modified,
            feed_language=feed_language,
        )
