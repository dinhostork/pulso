"""Wire serialization and bounded request validation for Bookmark HTTP adapters."""

from rest_framework import serializers


class BookmarkListQuerySerializer(serializers.Serializer):
    cursor = serializers.CharField(required=False, allow_blank=False, max_length=4096)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50, default=20)


def _timestamp(value):
    return serializers.DateTimeField().to_representation(value)


def serialize_story_card(card, *, bookmarked: bool) -> dict:
    return {
        "id": card.id,
        "language": card.language,
        "created_at": _timestamp(card.created_at),
        "first_published_at": (
            _timestamp(card.first_published_at) if card.first_published_at else None
        ),
        "last_published_at": (
            _timestamp(card.last_published_at) if card.last_published_at else None
        ),
        "content_state": card.content_state,
        "synthesis_id": card.synthesis_id,
        "synthesized_at": _timestamp(card.synthesized_at) if card.synthesized_at else None,
        "title": card.title,
        "elements": [
            {
                "id": element.id,
                "kind": element.kind,
                "position": element.position,
                "text": element.text,
                "article_ids": list(element.article_ids),
            }
            for element in card.elements
        ],
        "topics": [{"slug": topic.slug, "label": topic.label} for topic in card.topics],
        "article_count": card.article_count,
        "source_count": card.source_count,
        "viewer": {"bookmarked": bookmarked},
    }


def serialize_saved_entry(entry) -> dict:
    base = {"story_id": entry.story_id, "saved_at": _timestamp(entry.saved_at)}
    if entry.story is None:
        return {**base, "availability": "UNAVAILABLE"}
    return {
        **base,
        "availability": "AVAILABLE",
        "story": serialize_story_card(entry.story, bookmarked=True),
    }
