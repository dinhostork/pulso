"""Explicit wire serialization of News factual read DTOs (#41, #47).

Every field is listed by name: nothing is serialized from a model or DTO
wholesale, so raw Article bodies, RawArticle payloads, matcher evidence,
vectors, signatures and provider diagnostics cannot reach a response. Viewer
state is added by Reading, never here.
"""

from rest_framework import serializers

from api.http import PageQuerySerializer, ProductIdField


def timestamp(value):
    return serializers.DateTimeField().to_representation(value) if value is not None else None


def _elements(elements) -> list[dict]:
    return [
        {
            "id": element.id,
            "kind": element.kind,
            "position": element.position,
            "text": element.text,
            "article_ids": list(element.article_ids),
        }
        for element in elements
    ]


def story_card_facts(card) -> dict:
    return {
        "id": card.id,
        "language": card.language,
        "created_at": timestamp(card.created_at),
        "first_published_at": timestamp(card.first_published_at),
        "last_published_at": timestamp(card.last_published_at),
        "content_state": card.content_state,
        "synthesis_id": card.synthesis_id,
        "synthesized_at": timestamp(card.synthesized_at),
        "title": card.title,
        "elements": _elements(card.elements),
        "topics": [{"slug": topic.slug, "label": topic.label} for topic in card.topics],
        "article_count": card.article_count,
        "source_count": card.source_count,
    }


def source_article(article) -> dict:
    return {
        "id": article.id,
        "title": article.title,
        "canonical_url": article.canonical_url,
        "source": {
            "id": article.source.id,
            "name": article.source.name,
            "slug": article.source.slug,
        },
        "published_at": timestamp(article.published_at),
        "first_seen_at": timestamp(article.first_seen_at),
        "byline": article.byline,
        "duplicate_of_id": article.duplicate_of_id,
        "is_current_member": article.is_current_member,
    }


def story_detail_facts(detail) -> dict:
    return {
        **story_card_facts(detail),
        "entities": [
            {"kind": entity.kind, "display_name": entity.display_name} for entity in detail.entities
        ],
        "current_article_count": detail.current_article_count,
        "current_source_count": detail.current_source_count,
        "citations": {
            article_id: source_article(article) for article_id, article in detail.citations.items()
        },
        "sources_path": detail.sources_path,
    }


class StorySourcesQuerySerializer(PageQuerySerializer):
    synthesis_id = ProductIdField(required=False)
