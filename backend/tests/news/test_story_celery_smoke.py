"""Opt-in real-broker Story processing check (#29, #32, #34).

Redis delivers `embed_article_story` to a separately running worker, which
embeds the Article with the offline deterministic provider and dispatches
`match_article_story` itself; the resulting Story rows land in the database
this test asserts against. The complete-workflow test (#34) sends a later
same-event Article from another Source through the same worker and has the
worker refresh the Story, so embedding, matching, association and refresh all
cross Redis and a separate process. Run it exactly like the News smoke check
(`test_news_celery_smoke.py` and the worker infrastructure section of
backend/README.md); excluded from default `pytest` runs.
"""

import time

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from django.utils import timezone

from news.application.story_processing import current_keys
from news.domain.stories import member_signature
from news.models import (
    Article,
    ArticleEmbedding,
    ArticleStoryProcessing,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElementSource,
    StoryTopic,
)
from news.tasks import embed_article_story, refresh_story_task

TIMEOUT_SECONDS = 30


def wait_for(result):
    try:
        return result.get(timeout=TIMEOUT_SECONDS)
    except CeleryTimeoutError:
        pytest.fail(
            f"No worker consumed news.tasks.embed_article_story within {TIMEOUT_SECONDS}s; "
            "start one with the command documented in backend/README.md "
            "(config.settings_smoke_worker)"
        )


def wait_until_matched(article_id):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        row = ArticleStoryProcessing.objects.filter(article_id=article_id).first()
        if row is not None and row.state == ArticleStoryProcessing.State.MATCHED:
            return row
        time.sleep(0.2)
    pytest.fail("The worker did not complete match_article_story in time")


def make_article(number=1, slug="story-smoke"):
    source = Source.objects.create(slug=slug, name=slug.replace("-", " ").title())
    # A loopback literal needs no DNS lookup; the endpoint is never fetched.
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"http://127.0.0.1/{slug}.xml"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"story-smoke-{number}",
        external_id=f"story-smoke-{number}",
        url=f"https://story-smoke.example/{number}",
        payload={"title": "Harbor closes after storm damage"},
        payload_hash=f"{number:064x}",
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"story-smoke-{number}",
        canonical_url=f"https://story-smoke.example/{number}",
        title="Harbor closes after storm damage",
        body_text="The northern harbor closed on Monday after storm damage.",
        language="en",
        content_fingerprint=f"{number + 10:064x}",
        published_at=timezone.now(),
        first_seen_at=timezone.now(),
    )


@pytest.mark.celery_smoke
@pytest.mark.django_db(transaction=True)
def test_story_pipeline_runs_through_a_separately_running_worker():
    article = make_article()

    embedded = wait_for(embed_article_story.delay(article.pk))

    assert embedded["state"] == ArticleStoryProcessing.State.EMBEDDED
    assert embedded["pipeline_key"] == current_keys().pipeline_key
    row = wait_until_matched(article.pk)
    association = StoryArticle.objects.get(article=article, is_primary=True)
    assert (row.embedding_model_key, row.matcher_key) == (
        current_keys().embedding_model_key,
        current_keys().matcher_key,
    )
    assert association.method == StoryArticle.Method.CREATED_STORY

    # Redelivery through the worker is a no-op on a fresh Article.
    replay = wait_for(embed_article_story.delay(article.pk))
    assert replay["state"] == ArticleStoryProcessing.State.MATCHED
    assert StoryArticle.objects.filter(article=article).count() == 1
    assert ArticleEmbedding.objects.filter(article=article).count() == 1


@pytest.mark.celery_smoke
@pytest.mark.django_db(transaction=True)
def test_story_refresh_runs_through_a_separately_running_worker():
    article = make_article()
    story = Story.objects.create(language="en")
    StoryArticle.objects.create(
        story=story, article=article, is_primary=True, method=StoryArticle.Method.MANUAL
    )
    payload = refresh_story_task.delay(story.pk, reason="smoke").get(timeout=TIMEOUT_SECONDS)
    assert payload["outcome"] == "REFRESHED"
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.CURRENT
    assert StoryEmbedding.objects.get(story=story).member_signature == story.member_signature
    assert (
        StorySynthesis.objects.get(story=story, is_current=True).member_signature
        == story.member_signature
    )


@pytest.mark.celery_smoke
@pytest.mark.django_db(transaction=True)
def test_complete_story_workflow_runs_through_a_separately_running_worker():
    """#34: a second Source's same-event Article joins, and the worker refreshes."""

    first = make_article(1, "story-smoke")
    wait_for(embed_article_story.delay(first.pk))
    wait_until_matched(first.pk)
    story = StoryArticle.objects.get(article=first, is_primary=True).story
    first_refresh = wait_for(refresh_story_task.delay(story.pk, reason="membership_added"))
    assert first_refresh["outcome"] == "REFRESHED"
    first_synthesis = StorySynthesis.objects.get(story=story, is_current=True)

    second = make_article(2, "story-smoke-two")
    wait_for(embed_article_story.delay(second.pk))
    wait_until_matched(second.pk)

    story = StoryArticle.objects.get(article=first, is_primary=True).story
    joined = StoryArticle.objects.get(article=second, is_primary=True)
    assert joined.story_id == story.pk
    assert joined.method == StoryArticle.Method.MATCHED
    assert Story.objects.count() == 1
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE

    payload = wait_for(refresh_story_task.delay(story.pk, reason="membership_added"))

    assert payload["outcome"] == "REFRESHED"
    story.refresh_from_db()
    signature = member_signature(
        Article.objects.filter(pk__in=[first.pk, second.pk]).values_list("pk", "updated_at")
    )
    assert story.refresh_state == Story.RefreshState.CURRENT
    assert (story.member_signature, story.article_count, story.source_count) == (signature, 2, 2)
    embedding = StoryEmbedding.objects.get(story=story)
    assert (embedding.member_count, embedding.member_signature) == (2, signature)
    synthesis = StorySynthesis.objects.get(story=story, is_current=True)
    assert synthesis.member_signature == signature != first_synthesis.member_signature
    first_synthesis.refresh_from_db()
    assert not first_synthesis.is_current
    for model in (StoryTopic, StoryEntity):
        assert not model.objects.filter(story=story).exclude(member_signature=signature).exists()
    for element in synthesis.elements.all():
        assert list(element.sources.order_by("position").values_list("article_id", flat=True)) == [
            first.pk,
            second.pk,
        ]
    cited = set(
        StorySynthesisElementSource.objects.filter(element__synthesis=synthesis).values_list(
            "article_id", flat=True
        )
    )
    assert cited <= {first.pk, second.pk} and cited
    assert StoryArticle.objects.count() == 2
