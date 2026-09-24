"""Thin private HTTP adapters for Bookmark and FeedImpression application services."""

import json
import math
import time

from rest_framework import serializers, status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    ParseError,
    Throttled,
)
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from news.application.story_cursors import CursorError
from reading.application.bookmarks import (
    BookmarkStoryNotFound,
    BookmarkStoryUnavailable,
    list_bookmarks,
    remove_bookmark,
    save_bookmark,
)
from reading.application.impressions import (
    MAX_BODY_BYTES,
    ImpressionBatchInvalid,
    accept_feed_impressions,
    log_rejected_request,
)
from reading.serializers import (
    BookmarkListQuerySerializer,
    serialize_saved_entry,
)


def _error(code: str, detail: str, status_code: int, *, fields=None) -> Response:
    body = {"code": code, "detail": detail}
    if fields:
        body["fields"] = fields
    return Response(body, status=status_code)


class PrivateReadingAPIView(APIView):
    """Apply the private no-store policy even to controlled errors."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _error(
                "not_authenticated",
                "Authentication credentials were not provided.",
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, ParseError):
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"body": ["Malformed request body."]},
            )
        if isinstance(exc, Throttled):
            response = _error("rate_limited", "Try again later.", status.HTTP_429_TOO_MANY_REQUESTS)
            response["Retry-After"] = str(max(1, math.ceil(exc.wait or 1)))
            return response
        return super().handle_exception(exc)


class BookmarkListView(PrivateReadingAPIView):
    def get(self, request):
        allowed = {"cursor", "limit"}
        unknown = sorted(set(request.query_params) - allowed)
        if unknown:
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={name: ["Unknown field."] for name in unknown},
            )
        serializer = BookmarkListQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields=serializer.errors,
            )
        try:
            page = list_bookmarks(user=request.user, **serializer.validated_data)
        except CursorError:
            return _error(
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


class BookmarkMutationView(PrivateReadingAPIView):
    @staticmethod
    def _empty_body(request):
        return isinstance(request.data, dict) and not request.data

    def put(self, request, story_id: int):
        if not self._empty_body(request):
            fields = {
                str(name): ["Unknown field."]
                for name in getattr(request.data, "keys", lambda: [])()
            }
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields=fields or {"body": ["Expected an empty object."]},
            )
        try:
            result = save_bookmark(user=request.user, story_id=story_id)
        except ValueError:
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"story_id": ["Must be a positive integer."]},
            )
        except BookmarkStoryNotFound:
            return _error("story_not_found", "The Story was not found.", status.HTTP_404_NOT_FOUND)
        except BookmarkStoryUnavailable:
            return _error(
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
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields={"body": ["Expected an empty object."]},
            )
        try:
            remove_bookmark(user=request.user, story_id=story_id)
        except ValueError:
            return _error(
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
    return _error(
        "validation_error",
        "The request is invalid.",
        status.HTTP_400_BAD_REQUEST,
        fields={"body": [detail]},
    )


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


class FeedImpressionBatchView(PrivateReadingAPIView):
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
            return _error(
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
            return _error(
                "validation_error",
                "The request is invalid.",
                status.HTTP_400_BAD_REQUEST,
                fields=error.fields,
            )
        return Response(
            {
                "results": [
                    {"event_id": item.event_id, "outcome": item.outcome, "code": item.code}
                    for item in outcomes
                ]
            }
        )
