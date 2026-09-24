"""Feed composition and viewer decoration over user-independent News reads (#47).

News decides every factual field without knowing the account. Reading adds
only `viewer.bookmarked`, with one batched lookup after the factual snapshot,
so two accounts reading the same state receive identical facts.
"""

from dataclasses import dataclass

from news.application.story_read import (
    DEFAULT_PAGE_SIZE,
    Page,
    StoryCardDTO,
    StoryDetailDTO,
    feed_candidates,
    story_detail,
)
from reading.application.bookmarks import bookmarked_story_ids


@dataclass(frozen=True)
class ViewerFeedPage:
    page: Page[StoryCardDTO]
    bookmarked: frozenset[int]


@dataclass(frozen=True)
class ViewerStoryDetail:
    detail: StoryDetailDTO
    bookmarked: bool


def feed_for_viewer(
    *, user, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
) -> ViewerFeedPage:
    """The shared factual feed order (no personalization) plus this account's bookmarks."""

    page = feed_candidates(cursor=cursor, limit=limit)
    ids = tuple(int(card.id) for card in page.results)
    return ViewerFeedPage(
        page, bookmarked_story_ids(user=user, story_ids=ids) if ids else frozenset()
    )


def story_detail_for_viewer(*, user, story_id: int) -> ViewerStoryDetail:
    detail = story_detail(story_id)
    pk = int(detail.id)
    return ViewerStoryDetail(detail, pk in bookmarked_story_ids(user=user, story_ids=(pk,)))
