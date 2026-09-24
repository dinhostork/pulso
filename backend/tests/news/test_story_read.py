"""Coherent, bounded Story product reads against real PostgreSQL."""

import threading
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext

from news.application import story_read
from news.application.story_cursors import CursorError, decode_cursor, encode_cursor
from news.application.story_read import (
    FEED_SCOPE,
    SourceContextChanged,
    StoryNotFound,
    StoryUnavailable,
    feed_candidates,
    story_detail,
    story_is_save_eligible,
    story_sources,
)
from news.domain.stories import member_signature
from news.models import (
    Source,
    Story,
    StoryArticle,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
    StoryTopic,
    Topic,
)
from tests.news.story_read_builders import NOW, make_article, make_published_story


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.mark.django_db(transaction=True)
def test_multi_source_detail_and_feed_are_source_grounded_and_user_independent():
    first_source = Source.objects.create(slug="first-source", name="First Source")
    first = make_article(source=first_source, title="First", minutes=0)
    second = make_article(source=first_source, title="Second", minutes=1)
    third = make_article(title="Third", minutes=2)
    story, synthesis = make_published_story(first, second, third)

    page = feed_candidates()
    assert len(page.results) == 1
    card = page.results[0]
    assert (card.id, card.article_count, card.source_count) == (str(story.pk), 3, 2)
    assert card.synthesis_id == str(synthesis.pk)
    assert {article_id for element in card.elements for article_id in element.article_ids} == {
        str(first.pk),
        str(second.pk),
        str(third.pk),
    }
    assert not hasattr(card, "viewer")

    detail = story_detail(story.pk)
    assert detail.content_state == "CURRENT"
    assert (detail.current_article_count, detail.current_source_count) == (3, 2)
    assert set(detail.citations) == {str(first.pk), str(second.pk), str(third.pk)}
    assert all("body" not in vars(citation) for citation in detail.citations.values())
    assert detail.entities[0].display_name == "City Council"


@pytest.mark.django_db(transaction=True)
def test_one_article_story_is_eligible_and_freshness_matrix_is_exact():
    current, _ = make_published_story(make_article(), title="Current")
    stale, _ = make_published_story(
        make_article(), state=Story.RefreshState.STALE, title="Previous"
    )
    failed, _ = make_published_story(
        make_article(), state=Story.RefreshState.FAILED, title="Failed previous"
    )
    preparing_article = make_article()
    preparing = Story.objects.create(language="en")
    StoryArticle.objects.create(
        story=preparing,
        article=preparing_article,
        method=StoryArticle.Method.MANUAL,
        is_primary=True,
    )
    archived, _ = make_published_story(make_article(), status=Story.Status.ARCHIVED)
    empty = Story.objects.create(language="en")

    assert [card.id for card in feed_candidates(limit=50).results] == [str(current.pk)]
    assert story_detail(stale.pk).content_state == "UPDATING"
    assert story_detail(failed.pk).content_state == "UPDATING"
    preparing_detail = story_detail(preparing.pk)
    assert preparing_detail.content_state == "PREPARING"
    assert preparing_detail.title == "Story being prepared"
    assert preparing_detail.elements == () and preparing_detail.citations == {}
    for unavailable in (archived, empty):
        with pytest.raises(StoryUnavailable):
            story_detail(unavailable.pk)
    with pytest.raises(StoryNotFound):
        story_detail(999_999_999)


@pytest.mark.django_db(transaction=True)
def test_legacy_enrichment_is_omitted_and_cited_reassigned_articles_remain_reachable():
    article = make_article(title="Original title")
    remaining = make_article(title="Remaining current publication")
    story, _ = make_published_story(article, remaining)
    StoryTopic.objects.filter(story=story).update(member_signature=None)
    StoryEntity.objects.filter(story=story).update(member_signature=None)
    article.source.is_active = False
    article.source.save(update_fields=["is_active", "updated_at"])
    other, _ = make_published_story(make_article())
    StoryArticle.objects.filter(story=story, article=article).delete()
    StoryArticle.objects.create(
        story=other,
        article=article,
        method=StoryArticle.Method.MANUAL,
        is_primary=False,
    )
    Story.objects.filter(pk=story.pk).update(refresh_state=Story.RefreshState.STALE)

    detail = story_detail(story.pk)
    assert detail.content_state == "UPDATING"
    assert detail.topics == () and detail.entities == ()
    assert detail.citations[str(article.pk)].is_current_member is False
    assert detail.current_article_count == 1


@pytest.mark.django_db(transaction=True)
def test_feed_keyset_pagination_handles_ties_insertions_changes_and_exhaustion():
    tied = []
    for index in range(4):
        story, _ = make_published_story(make_article(), title=f"Story {index}")
        tied.append(story)
    Story.objects.filter(pk__in=[story.pk for story in tied]).update(created_at=NOW)
    expected = [str(story.pk) for story in sorted(tied, key=lambda row: row.pk, reverse=True)]

    first = feed_candidates(limit=2)
    assert [card.id for card in first.results] == expected[:2]
    assert first.next_cursor
    inserted, _ = make_published_story(make_article(), title="Inserted later")
    Story.objects.filter(pk=inserted.pk).update(created_at=NOW + timedelta(minutes=1))
    Story.objects.filter(pk=int(expected[2])).update(status=Story.Status.ARCHIVED)

    second = feed_candidates(cursor=first.next_cursor, limit=2)
    assert [card.id for card in second.results] == [expected[3]]
    assert str(inserted.pk) not in [card.id for card in second.results]
    assert second.next_cursor is None


@pytest.mark.django_db(transaction=True)
def test_cursor_rejects_tampering_expiry_scope_and_page_size():
    cursor = encode_cursor(
        scope=FEED_SCOPE,
        page_size=20,
        watermark=("2026-09-19T12:00:00Z", "10"),
        last=("2026-09-19T11:00:00Z", "9"),
        now=NOW,
    )
    assert (
        decode_cursor(
            cursor, expected_scope=FEED_SCOPE, page_size=20, now=NOW + timedelta(hours=23)
        ).last[1]
        == "9"
    )
    cases = [
        (cursor[:-1] + ("a" if cursor[-1] != "a" else "b"), FEED_SCOPE, 20, NOW),
        (cursor, FEED_SCOPE, 20, NOW + timedelta(hours=24)),
        (cursor, "other-scope", 20, NOW),
        (cursor, FEED_SCOPE, 10, NOW),
    ]
    for token, scope, size, now in cases:
        with pytest.raises(CursorError):
            decode_cursor(token, expected_scope=scope, page_size=size, now=now)
    for invalid in (0, 51, True):
        with pytest.raises(ValueError):
            feed_candidates(limit=invalid)
    account_cursor = encode_cursor(
        scope="saved",
        page_size=20,
        watermark=("2026-09-19T12:00:00Z", "10"),
        last=("2026-09-19T11:00:00Z", "9"),
        account_id="123",
        now=NOW,
    )
    with pytest.raises(CursorError):
        decode_cursor(
            account_cursor,
            expected_scope="saved",
            page_size=20,
            expected_account_id="456",
            now=NOW,
        )


@pytest.mark.django_db(transaction=True)
def test_source_cursor_is_ordered_and_rejects_changed_membership_context():
    articles = [make_article(minutes=index) for index in range(3)]
    story, synthesis = make_published_story(*articles)
    first = story_sources(story.pk, limit=2, synthesis_id=synthesis.pk)
    assert [int(row.id) for row in first.results] == sorted(article.pk for article in articles)[:2]
    assert first.next_cursor
    articles[-1].title = "Revised publication"
    articles[-1].save(update_fields=["title", "updated_at"])
    with pytest.raises(SourceContextChanged):
        story_sources(
            story.pk,
            cursor=first.next_cursor,
            limit=2,
            synthesis_id=synthesis.pk,
        )


@pytest.mark.django_db(transaction=True)
def test_query_counts_are_bounded_for_one_and_fifty_cards():
    make_published_story(make_article())
    with CaptureQueriesContext(connection) as small_queries:
        assert len(feed_candidates(limit=1).results) == 1
    for _ in range(49):
        make_published_story(make_article())
    with CaptureQueriesContext(connection) as large_queries:
        assert len(feed_candidates(limit=50).results) == 50
    assert len(small_queries) <= 12
    assert len(large_queries) == len(small_queries) <= 12

    sample = Story.objects.order_by("pk").first()
    with CaptureQueriesContext(connection) as detail_queries:
        story_detail(sample.pk)
    with CaptureQueriesContext(connection) as source_queries:
        story_sources(sample.pk)
    assert len(detail_queries) <= 12
    assert len(source_queries) <= 8


@pytest.mark.django_db(transaction=True)
def test_feed_order_index_has_query_plan_evidence():
    Story.objects.bulk_create(Story(language="en") for _ in range(1000))
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE news_story")
        cursor.execute(
            """
            EXPLAIN (COSTS OFF)
            SELECT id
            FROM news_story
            WHERE status = 'ACTIVE'
            ORDER BY created_at DESC, id DESC
            LIMIT 50
            """
        )
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "news_story_feed_order_idx" in plan


@pytest.mark.django_db(transaction=True)
def test_reads_execute_no_provider_task_or_write(monkeypatch):
    story, _ = make_published_story(make_article())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read invoked processing or dispatch")

    monkeypatch.setattr("news.application.story_refresh.refresh_story", forbidden)
    monkeypatch.setattr("news.tasks.refresh_story_task.delay", forbidden)
    with CaptureQueriesContext(connection) as queries:
        story_detail(story.pk)
    sql = " ".join(query["sql"].upper() for query in queries)
    assert "INSERT INTO" not in sql and "UPDATE " not in sql and "DELETE FROM" not in sql


@pytest.mark.django_db(transaction=True)
def test_repeatable_read_keeps_header_generation_enrichment_and_mutable_sources_coherent(
    monkeypatch,
):
    old_source = Source.objects.create(slug="old-source", name="Old Source")
    old_article = make_article(source=old_source, title="Old publication")
    second_old_article = make_article(title="Second old publication")
    story, old_synthesis = make_published_story(
        old_article, second_old_article, title="Old generation"
    )
    entered, resume = threading.Event(), threading.Event()
    result = []
    failures = []

    def checkpoint():
        entered.set()
        assert resume.wait(10)

    monkeypatch.setattr(story_read, "_snapshot_checkpoint", checkpoint)

    def reader():
        close_old_connections()
        try:
            result.append(story_detail(story.pk))
        except BaseException as error:
            failures.append(error)
        finally:
            close_old_connections()

    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(10)

    new_article = make_article(title="New publication")
    with transaction.atomic():
        old_source.name = "Renamed Source"
        old_source.save(update_fields=["name", "updated_at"])
        old_article.title = "Revised old publication"
        old_article.save(update_fields=["title", "updated_at"])
        StoryArticle.objects.filter(story=story).delete()
        StoryArticle.objects.create(
            story=story,
            article=new_article,
            is_primary=True,
            method=StoryArticle.Method.MANUAL,
        )
        signature = member_signature([(new_article.pk, new_article.updated_at)])
        StorySynthesis.objects.filter(story=story, is_current=True).update(is_current=False)
        new_synthesis = StorySynthesis.objects.create(
            story=story, model_key="test:read@2", member_signature=signature
        )
        title = StorySynthesisElement.objects.create(
            synthesis=new_synthesis,
            kind=StorySynthesisElement.Kind.TITLE,
            position=0,
            text="New generation",
        )
        StorySynthesisElementSource.objects.create(element=title, article=new_article, position=0)
        StoryTopic.objects.filter(story=story).delete()
        topic = Topic.objects.create(slug="new-topic", label="New Topic")
        StoryTopic.objects.create(
            story=story,
            topic=topic,
            score=1,
            model_key="test:topics@2",
            member_signature=signature,
        )
        Story.objects.filter(pk=story.pk).update(
            member_signature=signature,
            article_count=1,
            source_count=1,
            refresh_state=Story.RefreshState.CURRENT,
        )
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and failures == []
    snapshot = result[0]
    assert snapshot.synthesis_id == str(old_synthesis.pk)
    assert snapshot.title == "Old generation"
    assert snapshot.topics[0].slug == "public-policy"
    assert snapshot.citations[str(old_article.pk)].title == "Old publication"
    assert snapshot.citations[str(old_article.pk)].source.name == "Old Source"
    assert snapshot.current_article_count == 2

    monkeypatch.setattr(story_read, "_snapshot_checkpoint", lambda: None)
    current = story_detail(story.pk)
    assert current.title == "New generation"
    assert current.topics[0].slug == "new-topic"
    assert str(new_article.pk) in current.citations


@pytest.mark.django_db(transaction=True)
def test_save_eligibility_requires_caller_transaction():
    story, _ = make_published_story(make_article())
    with pytest.raises(RuntimeError):
        story_is_save_eligible(story)
    with transaction.atomic():
        locked = Story.objects.select_for_update().get(pk=story.pk)
        assert story_is_save_eligible(locked) is True


@pytest.mark.django_db(transaction=True)
def test_read_transaction_rejects_unsupported_nesting():
    story, _ = make_published_story(make_article())
    with transaction.atomic(), pytest.raises(RuntimeError):
        story_detail(story.pk)
