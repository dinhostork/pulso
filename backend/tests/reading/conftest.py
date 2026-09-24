from datetime import UTC, datetime

import pytest

from news.domain.stories import member_signature
from news.models import (
    Article,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def make_story():
    def factory(number: int, *, status=Story.Status.ACTIVE) -> tuple[Story, Article]:
        source = Source.objects.create(slug=f"reading-source-{number}", name=f"Source {number}")
        endpoint = SourceEndpoint.objects.create(
            source=source,
            kind=SourceEndpoint.Kind.RSS,
            url=f"https://reading-feed-{number}.example.org/rss",
        )
        raw = RawArticle.objects.create(
            endpoint=endpoint,
            external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
            external_key=f"reading-{number}",
            external_id=f"reading-{number}",
            url=f"https://reading-news-{number}.example.org/item",
            payload={"title": f"Reading article {number}"},
            payload_hash=f"{number:064d}",
            fetched_at=NOW,
        )
        article = Article.objects.create(
            source=source,
            endpoint=endpoint,
            raw_article=raw,
            external_id=f"reading-{number}",
            canonical_url=f"https://reading-news-{number}.example.org/item",
            title=f"Reading article {number}",
            body_text="Source-grounded body.",
            language="en",
            content_fingerprint=f"{number:064d}",
            first_seen_at=NOW,
        )
        story = Story.objects.create(language="en", status=status)
        StoryArticle.objects.create(
            story=story,
            article=article,
            method=StoryArticle.Method.MANUAL,
            is_primary=True,
        )
        return story, article

    return factory


@pytest.fixture
def publish_story():
    def publish(story, article, *, state=Story.RefreshState.CURRENT):
        signature = member_signature(((article.pk, article.updated_at),))
        Story.objects.filter(pk=story.pk).update(
            refresh_state=state,
            member_signature=signature,
            article_count=1,
            source_count=1,
            first_published_at=article.first_seen_at,
            last_published_at=article.first_seen_at,
            refreshed_at=NOW,
        )
        story.refresh_from_db()
        synthesis = StorySynthesis.objects.create(
            story=story,
            model_key="test:reading@1",
            member_signature=signature,
        )
        title = StorySynthesisElement.objects.create(
            synthesis=synthesis,
            kind=StorySynthesisElement.Kind.TITLE,
            position=0,
            text=f"Published Story {story.pk}",
        )
        StorySynthesisElementSource.objects.create(element=title, article=article, position=0)
        return story

    return publish
