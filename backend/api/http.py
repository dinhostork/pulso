"""The shared envelope of the private product HTTP adapters (#42, #43, #47).

Domain rules stay in their owning modules. This module only normalizes the
wire envelope: bounded `{code, detail, fields}` errors, the private no-store
cache policy and strict query/ID validation.
"""

import math
import re

from rest_framework import serializers, status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    ParseError,
    Throttled,
)
from rest_framework.response import Response
from rest_framework.views import APIView

MAX_PAGE_SIZE = 50
MAX_CURSOR_CHARS = 4096
_PRODUCT_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_PRODUCT_ID = 2**63 - 1
_LIMIT_MESSAGE = f"Must be between 1 and {MAX_PAGE_SIZE}."


def error_response(code: str, detail: str, status_code: int, *, fields=None) -> Response:
    body = {"code": code, "detail": detail}
    if fields:
        body["fields"] = fields
    return Response(body, status=status_code)


def validation_error(fields: dict) -> Response:
    return error_response(
        "validation_error",
        "The request is invalid.",
        status.HTTP_400_BAD_REQUEST,
        fields=fields,
    )


def parse_product_id(value: str) -> int | None:
    """A positive decimal ID within PostgreSQL bigint, without sign or leading zeros."""

    if not isinstance(value, str) or not _PRODUCT_ID.fullmatch(value):
        return None
    parsed = int(value)
    return parsed if parsed <= _MAX_PRODUCT_ID else None


class ProductIdField(serializers.CharField):
    default_error_messages = {"invalid_id": "Must be a positive decimal ID."}

    def to_internal_value(self, data):
        parsed = parse_product_id(super().to_internal_value(data))
        if parsed is None:
            self.fail("invalid_id")
        return parsed


class PageQuerySerializer(serializers.Serializer):
    cursor = serializers.CharField(required=False, allow_blank=False, max_length=MAX_CURSOR_CHARS)
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_PAGE_SIZE,
        default=20,
        error_messages={
            "invalid": _LIMIT_MESSAGE,
            "min_value": _LIMIT_MESSAGE,
            "max_value": _LIMIT_MESSAGE,
            "max_string_length": _LIMIT_MESSAGE,
        },
    )


def validated_query(request, serializer_class) -> tuple[dict | None, Response | None]:
    """Reject unknown query fields, then validate the known ones."""

    unknown = sorted(set(request.query_params) - set(serializer_class().fields))
    if unknown:
        return None, validation_error({name: ["Unknown field."] for name in unknown})
    serializer = serializer_class(data=request.query_params)
    if not serializer.is_valid():
        return None, validation_error(serializer.errors)
    return serializer.validated_data, None


class PrivateAPIView(APIView):
    """Apply the private no-store policy and stable error bodies, including to failures."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return error_response(
                "not_authenticated",
                "Authentication credentials were not provided.",
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, ParseError):
            return validation_error({"body": ["Malformed request body."]})
        if isinstance(exc, Throttled):
            response = error_response(
                "rate_limited", "Try again later.", status.HTTP_429_TOO_MANY_REQUESTS
            )
            response["Retry-After"] = str(max(1, math.ceil(exc.wait or 1)))
            return response
        return super().handle_exception(exc)
