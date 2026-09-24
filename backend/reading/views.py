"""Thin private HTTP adapters for Bookmark and FeedImpression application services."""

import json
import time

from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from api.http import (
    PrivateAPIView,
    error_response,
    parse_product_id,
    validated_query,
    validation_error,
)
from news.application.story_cursors import CursorError
from news.application.story_read import PersistedContractError
from news.views import INVALID_STORY_ID, READ_ERRORS, log_read, story_read_error
from reading.application.bookmarks import (
    BookmarkStoryNotFound,
    BookmarkStoryUnavailable,
    list_bookmarks,
    remove_bookmark,
    save_bookmark,
)
from reading.application.feed import feed_for_viewer, story_detail_for_viewer
from reading.application.impressions import (
    MAX_BODY_BYTES,
    ImpressionBatchInvalid,
    accept_feed_impressions,
    log_rejected_request,
)
from reading.serializers import (
    BookmarkListQuerySerializer,
    FeedQuerySerializer,
    serialize_feed_page,
    serialize_saved_entry,
    serialize_story_detail,
)


class BookmarkListView(PrivateAPIView):
    def get(self, request):
        query, invalid = validated_query(request, BookmarkListQuerySerializer)
        if invalid:
            return invalid
        try:
            page = list_bookmarks(user=request.user, **query)
        except CursorError:
            return error_response(
                "invalid_cursor",
                "The cursor is invalid or expired.",
                status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "results": [serialize_saved_entry(entry) for entry in page.results],
                "next_cursor": page.next_cursor,
            }
        )


class BookmarkMutationView(PrivateAPIView):
    @staticmethod
    def _empty_body(request):
        return isinstance(request.data, dict) and not request.data

    def put(self, request, story_id: int):
        if not self._empty_body(request):
            fields = {
                str(name): ["Unknown field."]
                for name in getattr(request.data, "keys", lambda: [])()
            }
            return error_response(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields=fields or {"body": ["Expected an empty object."]},
            )
        try:
            result = save_bookmark(user=request.user, story_id=story_id)
        except ValueError:
            return error_response(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"story_id": ["Must be a positive integer."]},
            )
        except BookmarkStoryNotFound:
            return error_response(
                "story_not_found", "The Story was not found.", status.HTTP_404_NOT_FOUND
            )
        except BookmarkStoryUnavailable:
            return error_response(
                "story_unavailable",
                "This Story is no longer available.",
                status.HTTP_410_GONE,
            )
        return Response(
            {
                "story_id": result.story_id,
                "bookmarked": True,
                "saved_at": serializers.DateTimeField().to_representation(result.saved_at),
            }
        )

    def delete(self, request, story_id: int):
        if not self._empty_body(request):
            return error_response(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"body": ["Expected an empty object."]},
            )
        try:
            remove_bookmark(user=request.user, story_id=story_id)
        except ValueError:
            return error_response(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"story_id": ["Must be a positive integer."]},
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class FeedImpressionThrottle(UserRateThrottle):
    """Advisory per-account batch rate; an operational bound, not fraud prevention."""

    scope = "feed_impressions"


def _malformed_body(detail: str) -> Response:
    return validation_error({"body": [detail]})


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


class FeedImpressionBatchView(PrivateAPIView):
    throttle_classes = [FeedImpressionThrottle]
    # The body is size-checked before it is decoded, so DRF parsers are unused.
    parser_classes = []

    def post(self, request):
        started = time.monotonic()
        media_type = (request.content_type or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            log_rejected_request("media_type", started)
            return _malformed_body("Expected application/json.")
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            log_rejected_request("content_length", started)
            return _malformed_body("Invalid Content-Length.")
        if declared > MAX_BODY_BYTES or len(body := request.body) > MAX_BODY_BYTES:
            log_rejected_request("body_too_large", started)
            return error_response(
                "request_too_large",
                "The request body is too large.",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except ValueError, RecursionError:
            log_rejected_request("malformed_json", started)
            return _malformed_body("Malformed request body.")
        try:
            outcomes = accept_feed_impressions(user=request.user, data=data, started=started)
        except ImpressionBatchInvalid as error:
            return validation_error(error.fields)
        return Response(
            {
                "results": [
                    {"event_id": item.event_id, "outcome": item.outcome, "code": item.code}
                    for item in outcomes
                ]
            }
        )


class FeedView(PrivateAPIView):
    """GET only: reading the feed never records an impression or triggers processing."""

    def get(self, request):
        started = time.monotonic()
        query, invalid = validated_query(request, FeedQuerySerializer)
        if invalid:
            log_read("feed_read", "invalid", started)
            return invalid
        try:
            feed = feed_for_viewer(user=request.user, **query)
        except (*READ_ERRORS, PersistedContractError) as error:
            log_read("feed_read", error.code, started)
            return story_read_error(error)
        log_read("feed_read", "success", started, len(feed.page.results))
        return Response(serialize_feed_page(feed))


class StoryDetailView(PrivateAPIView):
    def get(self, request, story_id: str):
        started = time.monotonic()
        pk = parse_product_id(story_id)
        if pk is None:
            log_read("story_detail_read", "invalid", started)
            return validation_error(INVALID_STORY_ID)
        if request.query_params:
            log_read("story_detail_read", "invalid", started)
            return validation_error(
                {name: ["Unknown field."] for name in sorted(request.query_params)}
            )
        try:
            viewed = story_detail_for_viewer(user=request.user, story_id=pk)
        except (*READ_ERRORS, PersistedContractError) as error:
            log_read("story_detail_read", error.code, started)
            return story_read_error(error)
        log_read("story_detail_read", "success", started, 1)
        return Response(serialize_story_detail(viewed.detail, bookmarked=viewed.bookmarked))
