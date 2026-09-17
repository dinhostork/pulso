"""Translate RSS 2.0, RSS 1.0/RDF and Atom through the News fetcher port.

Publication URL validation, normalization, persistence and deduplication belong
to later use cases.
"""

import calendar
import logging
from datetime import datetime, timezone

import feedparser

from news.application.ports import (
    EndpointFetchRequest,
    FetchedItem,
    FetcherPort,
    FetchError,
    FetchErrorKind,
    FetchResult,
    RejectedItem,
)

# Inside the `pulso` tree: this module parses source documents, so its
# operational records must go through the same JSON formatter and the same
# log-hygiene guarantee as the rest of News (#20).
LOGGER = logging.getLogger("pulso.news.adapters.rss")
RSS_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml, application/xml, text/xml"
)
MAX_RAW_LIST_ITEMS = 8
MAX_RAW_TEXT = 256


def _scalar(value: object, *, limit: int | None = MAX_RAW_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | int | float | bool):
        return None
    result = str(value)
    return result[:limit] if limit is not None and result else result or None


def _field(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _date(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    except TypeError, ValueError, OverflowError, OSError:
        return None


def _link(entry: dict) -> str | None:
    for link in entry.get("links", ()):
        if _field(link, "rel") == "alternate":
            href = _scalar(_field(link, "href"), limit=None)
            if href:
                return href
    return _scalar(entry.get("link"), limit=None)


def _content(entry: dict) -> str | None:
    choices = entry.get("content") or ()
    for accepted in (
        {"text/html", "html"},
        {"application/xhtml+xml", "xhtml"},
        {"text/plain", "text", "plain"},
    ):
        for part in choices:
            kind = _field(part, "type")
            if isinstance(kind, str) and kind.lower() in accepted:
                value = _field(part, "value")
                if isinstance(value, str):
                    return value
    return None


def _authors(entry: dict) -> tuple[str, ...]:
    names = []
    for author in entry.get("authors") or ():
        name = _scalar(_field(author, "name"), limit=None)
        if name:
            names.append(name)
    if not names:
        name = _scalar(entry.get("author"), limit=None)
        if name:
            names.append(name)
    return tuple(names)


def _language(entry: dict, feed_language: str | None) -> str | None:
    direct = _scalar(entry.get("language"), limit=None)
    if direct:
        return direct
    # feedparser exposes Atom entry xml:lang on parsed text/content details.
    for detail in (
        *entry.get("content", ()),
        entry.get("summary_detail"),
        entry.get("title_detail"),
    ):
        value = _scalar(_field(detail, "language"), limit=None)
        if value and value != feed_language:
            return value
    return feed_language


def _raw(entry: dict, date_errors: list[str]) -> dict[str, object]:
    raw: dict[str, object] = {}
    for key, fields in (
        ("tags", ("term", "scheme", "label")),
        ("enclosures", ("href", "type", "length")),
    ):
        values = []
        for part in (entry.get(key) or ())[:MAX_RAW_LIST_ITEMS]:
            selected = {
                field: text
                for field in fields
                if (text := _scalar(_field(part, field))) is not None
            }
            if selected:
                values.append(selected)
        if values:
            raw[key] = values
    source = entry.get("source")
    if source:
        selected = {
            field: text
            for field in ("title", "href", "link")
            if (text := _scalar(_field(source, field))) is not None
        }
        if selected:
            raw["source"] = selected
    guid = _scalar(entry.get("guid"))
    if guid:
        raw["guid"] = guid
    if "guidislink" in entry:
        raw["guidislink"] = bool(entry["guidislink"])
    if date_errors:
        raw["date_parse_error"] = ",".join(date_errors)
    return raw


def _item(entry: dict, feed_language: str | None) -> FetchedItem:
    published = _date(entry["published_parsed"] if "published_parsed" in entry else None)
    updated = _date(entry["updated_parsed"] if "updated_parsed" in entry else None)
    date_errors = []
    if "published" in entry and entry["published"] and published is None:
        date_errors.append("published")
    if "updated" in entry and entry["updated"] and updated is None:
        date_errors.append("updated")
    return FetchedItem(
        external_id=_scalar(entry.get("id") or entry.get("guid"), limit=None),
        url=_link(entry),
        title=_scalar(entry.get("title"), limit=None),
        summary_html=entry.get("summary") if isinstance(entry.get("summary"), str) else None,
        content_html=_content(entry),
        published_at=published or updated,
        updated_at=updated,
        authors=_authors(entry),
        language=_language(entry, feed_language),
        raw=_raw(entry, date_errors),
    )


class RssAdapter:
    """Map syndicated entries to FetchedItem without fetching or persisting them.

    FetchedItem field | feedparser source
    external_id      | id / guid
    url              | alternate link / link
    title            | title
    summary_html     | summary
    content_html     | preferred content value (HTML, XHTML, text)
    published_at     | published_parsed, falling back to updated_parsed (UTC)
    updated_at       | updated_parsed (UTC)
    authors          | authors / author
    language         | entry language, then feed language
    raw              | bounded tags, enclosures, source, guid and date marker
    """

    kind = "RSS"

    def fetch(self, request: EndpointFetchRequest, fetcher: FetcherPort) -> FetchResult:
        response = fetcher.get(
            request.url,
            accept=RSS_ACCEPT,
            etag=request.etag,
            last_modified=request.last_modified,
        )
        if response.not_modified:
            return FetchResult(
                not_modified=True, etag=response.etag, last_modified=response.last_modified
            )

        # Preserve suppressed-element boundaries for the deterministic domain
        # HTML parser. feedparser's sanitizer removes iframe tags but retains
        # fallback text, making that text indistinguishable from publication
        # content before normalization.
        parsed = feedparser.parse(
            response.body,
            response_headers=response.response_headers,
            sanitize_html=False,
        )
        entries = parsed.entries
        if parsed.get("bozo") and not entries:
            raise FetchError(FetchErrorKind.MALFORMED, "Syndication document is malformed")
        if parsed.get("bozo"):
            LOGGER.warning(
                "Syndication document has parse errors: adapter=%s entries=%d",
                self.kind,
                len(entries),
            )

        feed_language = _scalar(parsed.feed.get("language"), limit=None)
        items = []
        rejected = []
        for position, entry in enumerate(entries):
            try:
                item = _item(entry, feed_language)
            except TypeError, ValueError, AttributeError, OverflowError:
                rejected.append(RejectedItem(position=position, reason="MALFORMED_ENTRY"))
                continue
            if not (item.external_id or "").strip() and not (item.url or "").strip():
                rejected.append(RejectedItem(position=position, reason="MISSING_IDENTITY"))
            elif not any(
                value and value.strip()
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
