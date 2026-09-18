"""Extract and store a Story's Topics and Entities from its members (#30).

Input is strictly the Story's current membership: `StoryArticle` -> `Article`,
read in publication order (`published_at`, else `first_seen_at`, then id),
at most NEWS_STORY_ENRICHMENT_MAX_ARTICLES Articles and
NEWS_STORY_ENRICHMENT_MAX_CHARS_PER_ARTICLE characters each (title, then
description, then body — the #25 input rule). No other Story, no external
lookup and no knowledge base is consulted.

Replacement is compute-then-swap: extraction runs and is validated with no
transaction open; only then does one short transaction write the reference
rows and replace the Story's StoryTopic/StoryEntity sets. An extractor that
fails leaves the previous set exactly as it was. The (story, target)
uniqueness allows one current set per Story, so a new extraction replaces the
Story's rows whatever `model_key` produced them, and each row records its own.

Failures raise `EnrichmentError` and are logged with identifiers only. When
the service runs is decided by the Story refresh lifecycle (#32), not here.
"""

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from django.conf import settings
from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from news.adapters.rule_based_enrichment import RuleBasedEnrichmentExtractor
from news.application.story_ports import (
    ArticleText,
    EnrichmentError,
    EnrichmentErrorKind,
    EntityExtractor,
    ExtractedEntity,
    ExtractedTopic,
    StoryText,
    TopicExtractor,
)
from news.domain.embeddings import article_embedding_input
from news.domain.enrichment import normalize_name, slugify
from news.logging import ingestion_logger
from news.models import Article, Entity, Story, StoryEntity, StoryTopic, Topic

ENRICHMENT_LOGGER = "pulso.news.stories"


@dataclass(frozen=True)
class EnrichmentSummary:
    """Log-safe result: counts and model keys, never text or model instances."""

    story_id: int
    topic_count: int
    entity_count: int
    topic_model_key: str
    entity_model_key: str


@dataclass(frozen=True)
class ComputedEnrichment:
    topic_model_key: str
    entity_model_key: str
    topics: Mapping[str, tuple[str, float]]
    entities: Mapping[tuple[str, str], tuple[str, float]]


def default_extractor() -> RuleBasedEnrichmentExtractor:
    return RuleBasedEnrichmentExtractor(
        max_topics=settings.NEWS_STORY_MAX_TOPICS, max_entities=settings.NEWS_STORY_MAX_ENTITIES
    )


def story_text(story_id: int) -> StoryText:
    """The bounded text view of the Story's current members."""

    members = (
        Article.objects.filter(story_articles__story_id=story_id)
        .annotate(event_time=Coalesce("published_at", "first_seen_at"))
        .order_by("event_time", "pk")
        .values_list("pk", "title", "description", "body_text")[
            : settings.NEWS_STORY_ENRICHMENT_MAX_ARTICLES
        ]
    )
    return StoryText(
        story_id=story_id,
        articles=tuple(
            ArticleText(
                article_id=article_id,
                text=article_embedding_input(
                    title=title,
                    description=description,
                    body_text=body_text,
                    max_chars=settings.NEWS_STORY_ENRICHMENT_MAX_CHARS_PER_ARTICLE,
                ),
            )
            for article_id, title, description, body_text in members
        ),
    )


def story_text_from_members(story_id: int, members: tuple[object, ...]) -> StoryText:
    """Apply #30's ordering and bounds to captured refresh members."""

    ordered = sorted(members, key=lambda member: (member.event_time, member.article_id))
    return StoryText(
        story_id=story_id,
        articles=tuple(
            ArticleText(
                article_id=member.article_id,
                text=article_embedding_input(
                    title=member.title,
                    description=member.description,
                    body_text=member.body_text,
                    max_chars=settings.NEWS_STORY_ENRICHMENT_MAX_CHARS_PER_ARTICLE,
                ),
            )
            for member in ordered[: settings.NEWS_STORY_ENRICHMENT_MAX_ARTICLES]
        ),
    )


def _call(extract, story: StoryText, model_key: str):
    try:
        return tuple(extract(story))
    except EnrichmentError:
        raise
    except Exception:
        # Third-party errors may carry input text; keep none of it.
        raise EnrichmentError(
            EnrichmentErrorKind.EXTRACTOR_FAILED, "Extractor failed.", model_key=model_key
        ) from None


def _invalid(message: str, model_key: str) -> EnrichmentError:
    return EnrichmentError(EnrichmentErrorKind.INVALID_OUTPUT, message, model_key=model_key)


def _valid_score(score: object) -> bool:
    return isinstance(score, int | float) and math.isfinite(score) and 0 <= score <= 1


def _checked_topics(topics, model_key: str) -> dict[str, tuple[str, float]]:
    if len(topics) > settings.NEWS_STORY_MAX_TOPICS:
        raise _invalid("Extractor returned more Topics than allowed.", model_key)
    by_slug: dict[str, tuple[str, float]] = {}
    for topic in topics:
        if not isinstance(topic, ExtractedTopic) or not _valid_score(topic.score):
            raise _invalid("Extractor returned a malformed Topic.", model_key)
        slug = slugify(topic.label)[:100]
        if not slug:
            raise _invalid("Extractor returned a Topic without a usable name.", model_key)
        label = " ".join(topic.label.split())[:100]
        previous = by_slug.get(slug)
        by_slug[slug] = (label, max(topic.score, previous[1] if previous else 0.0))
    return by_slug


def _checked_entities(entities, model_key: str) -> dict[tuple[str, str], tuple[str, float]]:
    if len(entities) > settings.NEWS_STORY_MAX_ENTITIES:
        raise _invalid("Extractor returned more Entities than allowed.", model_key)
    by_key: dict[tuple[str, str], tuple[str, float]] = {}
    for entity in entities:
        if (
            not isinstance(entity, ExtractedEntity)
            or entity.kind not in Entity.Kind.values
            or not _valid_score(entity.score)
        ):
            raise _invalid("Extractor returned a malformed Entity.", model_key)
        key = normalize_name(entity.name)[:200]
        if not key:
            raise _invalid("Extractor returned an Entity without a usable name.", model_key)
        name = " ".join(entity.name.split())[:200]
        previous = by_key.get((entity.kind, key))
        by_key[(entity.kind, key)] = (
            previous[0] if previous else name,
            max(entity.score, previous[1] if previous else 0.0),
        )
    return by_key


def _fail(story_id: int, error: EnrichmentError) -> EnrichmentError:
    ingestion_logger(ENRICHMENT_LOGGER).warning(
        "News Story enrichment failed",
        extra={"story_id": story_id, "error_kind": str(error.kind), "model_key": error.model_key},
    )
    return error


def compute_story_enrichment(
    text: StoryText,
    *,
    topic_extractor: TopicExtractor | None = None,
    entity_extractor: EntityExtractor | None = None,
) -> ComputedEnrichment:
    """Validate extraction from prepared text without reading or writing the database."""

    topic_extractor = topic_extractor or default_extractor()
    entity_extractor = entity_extractor or default_extractor()
    topic_key = topic_extractor.identity.model_key
    entity_key = entity_extractor.identity.model_key
    try:
        if not text.articles:
            raise EnrichmentError(
                EnrichmentErrorKind.NO_MEMBERS, "Story has no member Articles.", model_key=topic_key
            )
        topics = _checked_topics(_call(topic_extractor.extract_topics, text, topic_key), topic_key)
        entities = _checked_entities(
            _call(entity_extractor.extract_entities, text, entity_key), entity_key
        )
    except EnrichmentError as error:
        raise _fail(text.story_id, error) from None

    return ComputedEnrichment(
        topic_key, entity_key, MappingProxyType(topics), MappingProxyType(entities)
    )


def persist_story_enrichment(
    story_id: int, computed: ComputedEnrichment, signature: str | None = None
) -> EnrichmentSummary:
    """Replace one Story's enrichment set; caller may provide an enclosing transaction."""

    generated_at = timezone.now()
    with transaction.atomic():
        topic_rows = {
            slug: Topic.objects.get_or_create(slug=slug, defaults={"label": label})[0]
            for slug, (label, _score) in computed.topics.items()
        }
        entity_rows = {
            key: Entity.objects.get_or_create(
                kind=key[0], normalized_key=key[1], defaults={"display_name": name}
            )[0]
            for key, (name, _score) in computed.entities.items()
        }
        StoryTopic.objects.filter(story_id=story_id).delete()
        StoryEntity.objects.filter(story_id=story_id).delete()
        StoryTopic.objects.bulk_create(
            StoryTopic(
                story_id=story_id,
                topic=topic_rows[slug],
                score=score,
                model_key=computed.topic_model_key,
                member_signature=signature,
                generated_at=generated_at,
            )
            for slug, (_label, score) in computed.topics.items()
        )
        StoryEntity.objects.bulk_create(
            StoryEntity(
                story_id=story_id,
                entity=entity_rows[key],
                score=score,
                model_key=computed.entity_model_key,
                member_signature=signature,
                generated_at=generated_at,
            )
            for key, (_name, score) in computed.entities.items()
        )
    summary = EnrichmentSummary(
        story_id,
        len(computed.topics),
        len(computed.entities),
        computed.topic_model_key,
        computed.entity_model_key,
    )
    ingestion_logger(ENRICHMENT_LOGGER).info(
        "News Story enrichment completed",
        extra={
            "story_id": story_id,
            "topic_count": summary.topic_count,
            "entity_count": summary.entity_count,
            "model_key": computed.entity_model_key,
        },
    )
    return summary


def extract_story_enrichment(
    story_id: int,
    *,
    topic_extractor: TopicExtractor | None = None,
    entity_extractor: EntityExtractor | None = None,
) -> EnrichmentSummary:
    """Replace the Story's Topics and Entities with a fresh extraction."""

    Story.objects.only("pk").get(pk=story_id)
    computed = compute_story_enrichment(
        story_text(story_id), topic_extractor=topic_extractor, entity_extractor=entity_extractor
    )
    return persist_story_enrichment(story_id, computed)
