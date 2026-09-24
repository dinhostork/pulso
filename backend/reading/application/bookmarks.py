"""Private Bookmark writes, pagination and viewer decoration."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar

from django.db import IntegrityError, transaction
from django.db.models import Q

from news.application.story_cursors import CursorError, decode_cursor, encode_cursor
from news.application.story_read import StoryCardDTO, story_cards, story_is_save_eligible
from news.models import Story
from reading.models import Bookmark

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
SAVED_SCOPE = "reading.saved.created-desc.v1"
UNIQUE_CONSTRAINT = "reading_bookmark_user_story_unique"
logger = logging.getLogger("pulso.reading.bookmarks")


class BookmarkError(RuntimeError):
    code = "bookmark_error"


class BookmarkStoryNotFound(BookmarkError):
    code = "story_not_found"


class BookmarkStoryUnavailable(BookmarkError):
    code = "story_unavailable"


@dataclass(frozen=True)
class BookmarkResult:
    story_id: str
    saved_at: datetime


@dataclass(frozen=True)
class SavedEntry:
    story_id: str
    saved_at: datetime
    story: StoryCardDTO | None


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    results: tuple[T, ...]
    next_cursor: str | None


def _page_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _positive_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("story_id must be a positive integer")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except TypeError, ValueError:
        raise CursorError() from None
    if parsed.tzinfo is None:
        raise CursorError()
    return parsed


def _is_unique_violation(error: IntegrityError) -> bool:
    cause = error.__cause__
    diagnostic = getattr(cause, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == UNIQUE_CONSTRAINT


def save_bookmark(*, user, story_id: int) -> BookmarkResult:
    """Set a Bookmark idempotently while serializing against Story lifecycle writes."""

    pk = _positive_id(story_id)
    with transaction.atomic():
        try:
            story = Story.objects.select_for_update().get(pk=pk)
        except Story.DoesNotExist:
            raise BookmarkStoryNotFound() from None
        existing = Bookmark.objects.filter(user=user, story=story).first()
        if existing is not None:
            logger.info(
                "Bookmark save completed",
                extra={"operation": "bookmark_save", "outcome": "existing", "count": 1},
            )
            return BookmarkResult(str(pk), existing.created_at)
        if not story_is_save_eligible(story):
            raise BookmarkStoryUnavailable()
        try:
            with transaction.atomic():
                bookmark = Bookmark.objects.create(user=user, story=story)
        except IntegrityError as error:
            if not _is_unique_violation(error):
                raise
            bookmark = Bookmark.objects.get(user=user, story=story)
        logger.info(
            "Bookmark save completed",
            extra={"operation": "bookmark_save", "outcome": "saved", "count": 1},
        )
        return BookmarkResult(str(pk), bookmark.created_at)


def remove_bookmark(*, user, story_id: int) -> None:
    """Remove only this account's Bookmark; absence is also success."""

    pk = _positive_id(story_id)
    with transaction.atomic():
        story = Story.objects.select_for_update().filter(pk=pk).first()
        deleted = 0
        if story is not None:
            deleted, _ = Bookmark.objects.filter(user=user, story=story).delete()
    logger.info(
        "Bookmark remove completed",
        extra={
            "operation": "bookmark_remove",
            "outcome": "removed" if deleted else "absent",
            "count": min(deleted, 1),
        },
    )


def list_bookmarks(
    *, user, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
) -> Page[SavedEntry]:
    """Return an account-bound keyset page, composing factual cards in one batch."""

    size = _page_size(limit)
    account_id = str(user.pk)
    decoded = (
        decode_cursor(
            cursor,
            expected_scope=SAVED_SCOPE,
            page_size=size,
            expected_account_id=account_id,
        )
        if cursor
        else None
    )
    rows = Bookmark.objects.filter(user=user).order_by("-created_at", "-pk")
    if decoded:
        try:
            watermark_at = _parse_datetime(decoded.watermark[0])
            watermark_id = int(decoded.watermark[1])
            last_at = _parse_datetime(decoded.last[0])
            last_id = int(decoded.last[1])
        except ValueError:
            raise CursorError() from None
        rows = rows.filter(
            Q(created_at__lt=watermark_at) | Q(created_at=watermark_at, pk__lte=watermark_id)
        ).filter(Q(created_at__lt=last_at) | Q(created_at=last_at, pk__lt=last_id))
    selected = list(rows[: size + 1])
    visible = selected[:size]
    cards = story_cards(tuple(row.story_id for row in visible))
    results = tuple(
        SavedEntry(str(row.story_id), row.created_at, cards.get(row.story_id)) for row in visible
    )
    next_cursor = None
    if len(selected) > size:
        first = visible[0]
        last = visible[-1]
        watermark = decoded.watermark if decoded else (_iso(first.created_at), str(first.pk))
        next_cursor = encode_cursor(
            scope=SAVED_SCOPE,
            page_size=size,
            watermark=watermark,
            last=(_iso(last.created_at), str(last.pk)),
            account_id=account_id,
        )
    logger.info(
        "Bookmark list completed",
        extra={"operation": "bookmark_list", "outcome": "success", "count": len(results)},
    )
    return Page(results, next_cursor)


def bookmarked_story_ids(*, user, story_ids: list[int] | tuple[int, ...]) -> frozenset[int]:
    """Resolve viewer decoration for one bounded Story page with one query."""

    if len(story_ids) > MAX_PAGE_SIZE:
        raise ValueError(f"story_ids cannot contain more than {MAX_PAGE_SIZE} items")
    ids = tuple(dict.fromkeys(_positive_id(value) for value in story_ids))
    return frozenset(
        Bookmark.objects.filter(user=user, story_id__in=ids).values_list("story_id", flat=True)
    )
