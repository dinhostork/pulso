"""Story/Article builders for product-read tests (#41, #47), against real PostgreSQL."""

from datetime import UTC, datetime, timedelta

from news.domain.stories import member_signature
from news.models import (
    Article,
    Entity,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
    StoryTopic,
    Topic,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
_numbers = iter(range(1, 100_000))


def make_article(*, source=None, title=None, minutes=0):
    number = next(_numbers)
    source = source or Source.objects.create(slug=f"read-source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source,
        kind=SourceEndpoint.Kind.RSS,
        url=f"https://feed-{number}.example.org/rss",
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"read-{number}",
        external_id=f"read-{number}",
        url=f"https://news-{number}.example.org/item",
        payload={"title": title or f"Article {number}"},
        payload_hash=f"{number:064d}",
        fetched_at=NOW,
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"read-{number}",
        canonical_url=f"https://news-{number}.example.org/item",
        title=title or f"Article {number}",
        body_text="Bounded factual body.",
        byline="Reporter",
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=NOW + timedelta(minutes=minutes),
        first_seen_at=NOW,
    )


def make_published_story(
    *articles,
    state=Story.RefreshState.CURRENT,
    status=Story.Status.ACTIVE,
    title="Published Story",
):
    story = Story.objects.create(language="en", status=status)
    for article in articles:
        StoryArticle.objects.create(
            story=story,
            article=article,
            is_primary=True,
            method=StoryArticle.Method.MANUAL,
        )
    signature = member_signature((article.pk, article.updated_at) for article in articles)
    published = [article.published_at or article.first_seen_at for article in articles]
    Story.objects.filter(pk=story.pk).update(
        refresh_state=state,
        member_signature=signature,
        article_count=len(articles),
        source_count=len({article.source_id for article in articles}),
        first_published_at=min(published) if published else None,
        last_published_at=max(published) if published else None,
        refreshed_at=NOW,
    )
    story.refresh_from_db()
    synthesis = StorySynthesis.objects.create(
        story=story, model_key="test:read@1", member_signature=signature
    )
    title_row = StorySynthesisElement.objects.create(
        synthesis=synthesis,
        kind=StorySynthesisElement.Kind.TITLE,
        position=0,
        text=title,
    )
    for position, article in enumerate(articles):
        StorySynthesisElementSource.objects.create(
            element=title_row, article=article, position=position
        )
    if articles:
        summary = StorySynthesisElement.objects.create(
            synthesis=synthesis,
            kind=StorySynthesisElement.Kind.SUMMARY,
            position=0,
            text="A source-grounded summary.",
        )
        StorySynthesisElementSource.objects.create(
            element=summary, article=articles[-1], position=0
        )
    topic, _ = Topic.objects.get_or_create(
        slug="public-policy", defaults={"label": "Public Policy"}
    )
    StoryTopic.objects.create(
        story=story,
        topic=topic,
        score=0.9,
        model_key="test:topics@1",
        member_signature=signature,
    )
    entity, _ = Entity.objects.get_or_create(
        kind=Entity.Kind.ORGANIZATION,
        normalized_key="city-council",
        defaults={"display_name": "City Council"},
    )
    StoryEntity.objects.create(
        story=story,
        entity=entity,
        score=0.8,
        model_key="test:entities@1",
        member_signature=signature,
    )
    return story, synthesis
