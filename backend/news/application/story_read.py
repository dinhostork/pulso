"""User-independent, coherent product reads for persisted Stories.

The current persistence model cannot anchor a complete response to only a
StorySynthesis id: Story headers, Topic/Entity links, current membership,
Articles and Sources are mutable or replaced independently. Every public
entrypoint therefore fully materializes its DTO inside one short PostgreSQL
REPEATABLE READ, READ ONLY transaction.
"""

import ipaddress
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q

from news.application.story_cursors import CursorError, decode_cursor, encode_cursor
from news.application.story_synthesis import KIND_ORDER, MAX_ELEMENT_CHARS, MAX_ELEMENTS
from news.domain.stories import member_signature
from news.models import (
    Article,
    Story,
    StoryArticle,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
    StoryTopic,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_PUBLICATION_TITLE_CHARS = 500
MAX_BYLINE_CHARS = 512
FEED_SCOPE = "news.feed.created-desc.v1"
logger = logging.getLogger("pulso.news.stories")


class StoryReadError(RuntimeError):
    code = "story_read_error"


class StoryNotFound(StoryReadError):
    code = "story_not_found"


class StoryUnavailable(StoryReadError):
    code = "story_unavailable"


class SourceContextChanged(StoryReadError):
    code = "source_context_changed"


class PersistedContractError(StoryReadError):
    code = "invalid_story_generation"


@dataclass(frozen=True)
class TopicDTO:
    slug: str
    label: str


@dataclass(frozen=True)
class EntityDTO:
    kind: str
    display_name: str


@dataclass(frozen=True)
class StoryElementDTO:
    id: str
    kind: str
    position: int
    text: str
    article_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceDTO:
    id: str
    name: str
    slug: str


@dataclass(frozen=True)
class SourceArticleDTO:
    id: str
    title: str
    canonical_url: str
    source: SourceDTO
    published_at: datetime | None
    first_seen_at: datetime
    byline: str | None
    duplicate_of_id: str | None
    is_current_member: bool


@dataclass(frozen=True)
class StoryCardDTO:
    id: str
    language: str
    created_at: datetime
    first_published_at: datetime | None
    last_published_at: datetime | None
    content_state: str
    synthesis_id: str
    synthesized_at: datetime
    title: str
    elements: tuple[StoryElementDTO, ...]
    topics: tuple[TopicDTO, ...]
    article_count: int
    source_count: int


@dataclass(frozen=True)
class StoryDetailDTO:
    id: str
    language: str
    created_at: datetime
    first_published_at: datetime | None
    last_published_at: datetime | None
    content_state: str
    synthesis_id: str | None
    synthesized_at: datetime | None
    title: str
    elements: tuple[StoryElementDTO, ...]
    topics: tuple[TopicDTO, ...]
    entities: tuple[EntityDTO, ...]
    article_count: int
    source_count: int
    current_article_count: int
    current_source_count: int
    citations: dict[str, SourceArticleDTO]
    sources_path: str


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    results: tuple[T, ...]
    next_cursor: str | None
    ordering: str | None = None


def _page_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _positive_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@contextmanager
def _factual_snapshot():
    """Own the complete short product-read transaction explicitly."""

    if connection.in_atomic_block:
        raise RuntimeError("News factual reads cannot run inside another transaction")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield


def _snapshot_checkpoint() -> None:
    """Deterministic concurrency-test seam; intentionally does nothing."""


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


def _safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(character) < 32 for character in value)
        ):
            return False
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        )
    except ValueError:
        return False


def _source_article(article: Article, current_ids: set[int]) -> SourceArticleDTO:
    if not _safe_url(article.canonical_url):
        raise PersistedContractError("unsafe publication URL")
    byline = " ".join(article.byline.split())[:MAX_BYLINE_CHARS] or None
    return SourceArticleDTO(
        id=str(article.pk),
        title=" ".join(article.title.split())[:MAX_PUBLICATION_TITLE_CHARS],
        canonical_url=article.canonical_url,
        source=SourceDTO(str(article.source_id), article.source.name, article.source.slug),
        published_at=article.published_at,
        first_seen_at=article.first_seen_at,
        byline=byline,
        duplicate_of_id=str(article.duplicate_of_id) if article.duplicate_of_id else None,
        is_current_member=article.pk in current_ids,
    )


def _prefetched_syntheses(story_ids: list[int]) -> dict[int, StorySynthesis]:
    source_rows = StorySynthesisElementSource.objects.order_by("position")
    elements = StorySynthesisElement.objects.order_by(
        KIND_ORDER, "position", "pk"
    ).prefetch_related(Prefetch("sources", queryset=source_rows, to_attr="ordered_sources"))
    rows = (
        StorySynthesis.objects.filter(story_id__in=story_ids, is_current=True)
        .order_by("story_id", "pk")
        .prefetch_related(Prefetch("elements", queryset=elements, to_attr="ordered_elements"))
    )
    return {row.story_id: row for row in rows}


def _elements(synthesis: StorySynthesis) -> tuple[StoryElementDTO, ...] | None:
    rows = synthesis.ordered_elements
    if not 1 <= len(rows) <= MAX_ELEMENTS:
        return None
    titles = [row for row in rows if row.kind == StorySynthesisElement.Kind.TITLE]
    if len(titles) != 1:
        return None
    result = []
    for row in rows:
        sources = tuple(str(source.article_id) for source in row.ordered_sources)
        if not sources or not row.text or len(row.text) > MAX_ELEMENT_CHARS:
            return None
        result.append(StoryElementDTO(str(row.pk), row.kind, row.position, row.text, sources))
    return tuple(result)


def _topic_map(story_ids: list[int]) -> dict[int, list[StoryTopic]]:
    result: dict[int, list[StoryTopic]] = {}
    rows = (
        StoryTopic.objects.filter(story_id__in=story_ids)
        .select_related("topic")
        .order_by("story_id", "-score", "topic__slug")
    )
    for row in rows:
        result.setdefault(row.story_id, []).append(row)
    return result


def _entity_rows(story_id: int, signature: str) -> tuple[EntityDTO, ...]:
    rows = (
        StoryEntity.objects.filter(story_id=story_id, member_signature=signature)
        .select_related("entity")
        .order_by("-score", "entity__kind", "entity__normalized_key")
    )
    values = tuple(EntityDTO(row.entity.kind, row.entity.display_name) for row in rows)
    return values if len(values) <= settings.NEWS_STORY_MAX_ENTITIES else ()


def _topics(rows: list[StoryTopic], signature: str) -> tuple[TopicDTO, ...]:
    values = tuple(
        TopicDTO(row.topic.slug, row.topic.label)
        for row in rows
        if row.member_signature == signature
    )
    return values if len(values) <= settings.NEWS_STORY_MAX_TOPICS else ()


def _valid_generation(story: Story, synthesis: StorySynthesis | None):
    if (
        synthesis is None
        or not synthesis.member_signature
        or synthesis.member_signature != story.member_signature
    ):
        return None
    elements = _elements(synthesis)
    if elements is None:
        logger.warning(
            "Story generation rejected for product read",
            extra={
                "operation": "story_read",
                "error_code": "invalid_story_generation",
                "story_id": story.pk,
            },
        )
        return None
    return elements


def _card(
    story: Story,
    synthesis: StorySynthesis | None,
    topic_rows: list[StoryTopic],
) -> StoryCardDTO | None:
    elements = _valid_generation(story, synthesis)
    if elements is None or story.refresh_state != Story.RefreshState.CURRENT:
        return None
    return StoryCardDTO(
        id=str(story.pk),
        language=story.language,
        created_at=story.created_at,
        first_published_at=story.first_published_at,
        last_published_at=story.last_published_at,
        content_state="CURRENT",
        synthesis_id=str(synthesis.pk),
        synthesized_at=synthesis.generated_at,
        title=next(element.text for element in elements if element.kind == "TITLE"),
        elements=elements,
        topics=_topics(topic_rows, synthesis.member_signature),
        article_count=story.article_count,
        source_count=story.source_count,
    )


def feed_candidates(
    *, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
) -> Page[StoryCardDTO]:
    """Return CURRENT, nonempty Stories in deterministic creation order."""

    size = _page_size(limit)
    decoded = decode_cursor(cursor, expected_scope=FEED_SCOPE, page_size=size) if cursor else None
    current_synthesis = StorySynthesis.objects.filter(
        story_id=OuterRef("pk"), is_current=True, member_signature=OuterRef("member_signature")
    )
    live_member = StoryArticle.objects.filter(story_id=OuterRef("pk"))
    with _factual_snapshot():
        stories = (
            Story.objects.filter(
                status=Story.Status.ACTIVE,
                refresh_state=Story.RefreshState.CURRENT,
            )
            .annotate(has_generation=Exists(current_synthesis), has_live_member=Exists(live_member))
            .filter(has_generation=True, has_live_member=True)
            .order_by("-created_at", "-pk")
        )
        if decoded:
            try:
                watermark_at = _parse_datetime(decoded.watermark[0])
                watermark_id = int(decoded.watermark[1])
                last_at = _parse_datetime(decoded.last[0])
                last_id = int(decoded.last[1])
            except ValueError:
                raise CursorError() from None
            stories = stories.filter(
                Q(created_at__lt=watermark_at) | Q(created_at=watermark_at, pk__lte=watermark_id)
            ).filter(Q(created_at__lt=last_at) | Q(created_at=last_at, pk__lt=last_id))
        selected = list(stories[: size + 1])
        _snapshot_checkpoint()
        visible = selected[:size]
        ids = [story.pk for story in visible]
        syntheses = _prefetched_syntheses(ids)
        topics = _topic_map(ids)
        cards = tuple(
            card
            for story in visible
            if (card := _card(story, syntheses.get(story.pk), topics.get(story.pk, []))) is not None
        )
        next_cursor = None
        if len(selected) > size and cards:
            first = visible[0] if decoded is None else None
            watermark = decoded.watermark if decoded else (_iso(first.created_at), str(first.pk))
            last_story = next(story for story in reversed(visible) if str(story.pk) == cards[-1].id)
            next_cursor = encode_cursor(
                scope=FEED_SCOPE,
                page_size=size,
                watermark=watermark,
                last=(_iso(last_story.created_at), str(last_story.pk)),
            )
        return Page(cards, next_cursor, "story_created_desc_v1")


def story_detail(story_id: int) -> StoryDetailDTO:
    """Materialize detail, citations and current membership in one snapshot."""

    pk = _positive_id(story_id, "story_id")
    with _factual_snapshot():
        try:
            story = Story.objects.get(pk=pk)
        except Story.DoesNotExist:
            raise StoryNotFound() from None
        membership = list(
            StoryArticle.objects.filter(story_id=pk)
            .select_related("article")
            .values_list("article_id", "article__source_id")
        )
        if story.status != Story.Status.ACTIVE or not membership:
            raise StoryUnavailable()
        _snapshot_checkpoint()
        synthesis = _prefetched_syntheses([pk]).get(pk)
        elements = _valid_generation(story, synthesis)
        current_ids = {article_id for article_id, _ in membership}
        current_source_count = len({source_id for _, source_id in membership})
        if elements is None:
            return StoryDetailDTO(
                id=str(pk),
                language=story.language,
                created_at=story.created_at,
                first_published_at=story.first_published_at,
                last_published_at=story.last_published_at,
                content_state="PREPARING",
                synthesis_id=None,
                synthesized_at=None,
                title="Story being prepared",
                elements=(),
                topics=(),
                entities=(),
                article_count=0,
                source_count=0,
                current_article_count=len(current_ids),
                current_source_count=current_source_count,
                citations={},
                sources_path=f"/api/stories/{pk}/sources",
            )
        topic_rows = _topic_map([pk]).get(pk, [])
        article_ids = {int(value) for element in elements for value in element.article_ids}
        articles = (
            Article.objects.filter(pk__in=article_ids).select_related("source").order_by("pk")
        )
        citations = {str(article.pk): _source_article(article, current_ids) for article in articles}
        if set(citations) != {str(value) for value in article_ids}:
            raise PersistedContractError("missing cited publication")
        content_state = (
            "CURRENT" if story.refresh_state == Story.RefreshState.CURRENT else "UPDATING"
        )
        return StoryDetailDTO(
            id=str(pk),
            language=story.language,
            created_at=story.created_at,
            first_published_at=story.first_published_at,
            last_published_at=story.last_published_at,
            content_state=content_state,
            synthesis_id=str(synthesis.pk),
            synthesized_at=synthesis.generated_at,
            title=next(element.text for element in elements if element.kind == "TITLE"),
            elements=elements,
            topics=_topics(topic_rows, synthesis.member_signature),
            entities=_entity_rows(pk, synthesis.member_signature),
            article_count=story.article_count,
            source_count=story.source_count,
            current_article_count=len(current_ids),
            current_source_count=current_source_count,
            citations=citations,
            sources_path=f"/api/stories/{pk}/sources?synthesis_id={synthesis.pk}",
        )


def story_sources(
    story_id: int,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    synthesis_id: int | None = None,
) -> Page[SourceArticleDTO]:
    """Return current publications, with a membership-bound ascending cursor."""

    pk = _positive_id(story_id, "story_id")
    size = _page_size(limit)
    synthesis_pk = _positive_id(synthesis_id, "synthesis_id") if synthesis_id is not None else None
    scope = f"news.story-sources.{pk}"
    decoded = decode_cursor(cursor, expected_scope=scope, page_size=size) if cursor else None
    with _factual_snapshot():
        try:
            story = Story.objects.only("pk", "status").get(pk=pk)
        except Story.DoesNotExist:
            raise StoryNotFound() from None
        rows = list(
            Article.objects.filter(story_articles__story_id=pk)
            .select_related("source")
            .order_by("pk")
        )
        if story.status != Story.Status.ACTIVE or not rows:
            raise StoryUnavailable()
        signature = member_signature((article.pk, article.updated_at) for article in rows)
        context = f"{signature}:{synthesis_pk or ''}"
        if (
            synthesis_pk is not None
            and not StorySynthesis.objects.filter(pk=synthesis_pk, story_id=pk).exists()
        ):
            raise ValueError("synthesis_id does not belong to this Story")
        if decoded and decoded.context != context:
            raise SourceContextChanged()
        if decoded:
            if decoded.watermark[0] != "article-id" or decoded.last[0] != "article-id":
                raise CursorError()
            try:
                last_id = int(decoded.last[1])
            except ValueError:
                raise CursorError() from None
            rows = [article for article in rows if article.pk > last_id]
        selected = rows[: size + 1]
        current_ids = {article.pk for article in rows}
        results = tuple(_source_article(article, current_ids) for article in selected[:size])
        next_cursor = None
        if len(selected) > size:
            watermark = decoded.watermark if decoded else ("article-id", str(selected[0].pk))
            next_cursor = encode_cursor(
                scope=scope,
                page_size=size,
                watermark=watermark,
                last=("article-id", str(selected[size - 1].pk)),
                context=context,
            )
        return Page(results, next_cursor)


def story_is_save_eligible(story: Story) -> bool:
    """Narrow check for callers that already locked the Story in a write transaction."""

    if not connection.in_atomic_block:
        raise RuntimeError("Bookmark eligibility requires a caller-owned transaction")
    return story.status == Story.Status.ACTIVE and StoryArticle.objects.filter(story=story).exists()
