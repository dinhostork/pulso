"""Thin private HTTP adapters for Bookmark application services."""

from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, ParseError
from rest_framework.response import Response
from rest_framework.views import APIView

from news.application.story_cursors import CursorError
from reading.application.bookmarks import (
    BookmarkStoryNotFound,
    BookmarkStoryUnavailable,
    list_bookmarks,
    remove_bookmark,
    save_bookmark,
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
