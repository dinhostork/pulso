"""Shared resolution helpers for the News operator commands.

The leading underscore keeps this module out of Django's command discovery.
"""

from django.core.management.base import CommandError

from news.models import Article, Source, SourceEndpoint


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


def resolve_article(identifier) -> int:
    """Resolve an Article by numeric id; returns the id."""

    token = str(identifier or "").strip()
    if not token.isdigit():
        raise CommandError("An Article id is required")
    article_id = int(token)
    if not Article.objects.filter(pk=article_id).exists():
        raise CommandError(f"No Article with id {article_id}")
    return article_id


def story_step_line(result) -> str:
    """One line of identifiers, states and version keys — never Article text."""

    return (
        f"article_id={result.article_id} state={result.state} "
        f"story_id={result.story_id if result.story_id is not None else '-'} "
        f"attempts={result.attempts} error_kind={result.error_kind or '-'} "
        f"pipeline_key={result.pipeline_key}"
    )
