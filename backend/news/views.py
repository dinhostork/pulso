"""Thin private HTTP adapter for the user-independent Story source list (#47)."""

import logging
import time

from rest_framework import status
from rest_framework.response import Response

from api.http import (
    PrivateAPIView,
    error_response,
    parse_product_id,
    validated_query,
    validation_error,
)
from news.application.story_cursors import CursorError
from news.application.story_read import (
    PersistedContractError,
    SourceContextChanged,
    StoryNotFound,
    StoryUnavailable,
    story_sources,
)
from news.serializers import StorySourcesQuerySerializer, source_article

logger = logging.getLogger("pulso.news.http")

INVALID_STORY_ID = {"story_id": ["Must be a positive decimal ID."]}


def story_read_error(error: Exception) -> Response:
    """Translate a News read failure into its stable product error."""

    if isinstance(error, CursorError):
        return error_response(
            "invalid_cursor", "The cursor is invalid or expired.", status.HTTP_400_BAD_REQUEST
        )
    if isinstance(error, StoryNotFound):
        return error_response(
            "story_not_found", "The Story was not found.", status.HTTP_404_NOT_FOUND
        )
    if isinstance(error, StoryUnavailable):
        return error_response(
            "story_unavailable", "This Story is no longer available.", status.HTTP_410_GONE
        )
    if isinstance(error, SourceContextChanged):
        return error_response(
            "source_context_changed",
            "The source list changed; restart pagination.",
            status.HTTP_409_CONFLICT,
        )
    return error_response(
        "server_error",
        "The request could not be completed.",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


READ_ERRORS = (CursorError, StoryNotFound, StoryUnavailable, SourceContextChanged)


def log_read(operation: str, outcome: str, started: float, count: int = 0) -> None:
    logger.info(
        "Story read request completed",
        extra={
            "operation": operation,
            "outcome": outcome,
            "count": count,
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )


class StorySourcesView(PrivateAPIView):
    def get(self, request, story_id: str):
        started = time.monotonic()
        pk = parse_product_id(story_id)
        query, invalid = validated_query(request, StorySourcesQuerySerializer)
        if pk is None or invalid:
            log_read("story_sources_read", "invalid", started)
            if pk is None:
                return validation_error(INVALID_STORY_ID)
            return invalid
        try:
            page = story_sources(pk, **query)
        except (*READ_ERRORS, PersistedContractError) as error:
            # CursorError is also a ValueError, so these are translated first.
            log_read("story_sources_read", error.code, started)
            return story_read_error(error)
        except ValueError:
            # The synthesis ID is well formed but belongs to another Story.
            log_read("story_sources_read", "invalid", started)
            return validation_error({"synthesis_id": ["Does not belong to this Story."]})
        log_read("story_sources_read", "success", started, len(page.results))
        return Response(
            {
                "results": [source_article(article) for article in page.results],
                "next_cursor": page.next_cursor,
            }
        )
