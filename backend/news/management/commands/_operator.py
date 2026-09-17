"""Shared resolution helpers for the News operator commands.

The leading underscore keeps this module out of Django's command discovery.
"""

from django.core.management.base import CommandError

from news.models import Source, SourceEndpoint


def resolve_source(identifier: str) -> Source:
    """Resolve a Source by its unique slug."""

    slug = (identifier or "").strip()
    if not slug:
        raise CommandError("A Source slug is required")
    try:
        return Source.objects.get(slug=slug)
    except Source.DoesNotExist:
        raise CommandError(f"No Source with slug {slug!r}") from None


def resolve_endpoint(identifier: str) -> SourceEndpoint:
    """Resolve an endpoint by numeric id or by its exact, unique URL.

    Only a strictly integer token is treated as a primary key, so a URL is
    never matched partially or reinterpreted as an id.
    """

    token = (identifier or "").strip()
    if not token:
        raise CommandError("An endpoint id or exact URL is required")
    queryset = SourceEndpoint.objects.select_related("source")
    if token.isdigit():
        try:
            return queryset.get(pk=int(token))
        except SourceEndpoint.DoesNotExist:
            raise CommandError(f"No SourceEndpoint with id {token}") from None
    try:
        return queryset.get(url=token)
    except SourceEndpoint.DoesNotExist:
        raise CommandError(f"No SourceEndpoint with URL {token!r}") from None
