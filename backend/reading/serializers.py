"""Viewer-decorated wire serialization and request validation for Reading HTTP adapters."""

from rest_framework import serializers

from api.http import PageQuerySerializer
from news.serializers import story_card_facts, story_detail_facts


class BookmarkListQuerySerializer(PageQuerySerializer):
    pass


class FeedQuerySerializer(PageQuerySerializer):
    pass


def _timestamp(value):
    return serializers.DateTimeField().to_representation(value)


def serialize_story_card(card, *, bookmarked: bool) -> dict:
    return {**story_card_facts(card), "viewer": {"bookmarked": bookmarked}}


def serialize_story_detail(detail, *, bookmarked: bool) -> dict:
    return {**story_detail_facts(detail), "viewer": {"bookmarked": bookmarked}}


def serialize_feed_page(feed) -> dict:
    return {
        "results": [
            serialize_story_card(card, bookmarked=int(card.id) in feed.bookmarked)
            for card in feed.page.results
        ],
        "next_cursor": feed.page.next_cursor,
        "ordering": feed.page.ordering,
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
