import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext

from news.application.story_cursors import CursorError
from news.models import Article, RawArticle, Story
from reading.application.bookmarks import (
    BookmarkStoryNotFound,
    BookmarkStoryUnavailable,
    bookmarked_story_ids,
    list_bookmarks,
    remove_bookmark,
    save_bookmark,
)
from reading.models import Bookmark

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@pytest.fixture
def users(db):
    model = get_user_model()
    return model.objects.create_user(username="reader-a"), model.objects.create_user(
        username="reader-b"
    )


@pytest.mark.django_db(transaction=True)
def test_model_enforces_unique_owner_story_and_story_protection(users, make_story):
    user, _ = users
    story, _ = make_story(1)
    Bookmark.objects.create(user=user, story=story)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Bookmark.objects.create(user=user, story=story)
    with pytest.raises(ProtectedError):
        story.delete()

    user.delete()
    assert not Bookmark.objects.exists()
    story.delete()


@pytest.mark.django_db(transaction=True)
def test_save_is_idempotent_and_accepts_current_preparing_and_updating_stories(
    users, make_story, publish_story
):
    user, _ = users
    current, current_article = make_story(2)
    preparing, _ = make_story(3)
    updating, updating_article = make_story(4)
    publish_story(current, current_article)
    publish_story(updating, updating_article, state=Story.RefreshState.STALE)

    first = save_bookmark(user=user, story_id=current.pk)
    second = save_bookmark(user=user, story_id=current.pk)
    save_bookmark(user=user, story_id=preparing.pk)
    save_bookmark(user=user, story_id=updating.pk)

    assert first == second
    assert Bookmark.objects.filter(user=user).count() == 3
    states = {
        entry.story_id: entry.story.content_state for entry in list_bookmarks(user=user).results
    }
    assert states == {
        str(current.pk): "CURRENT",
        str(preparing.pk): "PREPARING",
        str(updating.pk): "UPDATING",
    }


@pytest.mark.django_db(transaction=True)
def test_missing_unavailable_and_archived_existing_bookmark_behaviors(users, make_story):
    user, _ = users
    with pytest.raises(BookmarkStoryNotFound):
        save_bookmark(user=user, story_id=999_999_999)
    archived, _ = make_story(5, status=Story.Status.ARCHIVED)
    with pytest.raises(BookmarkStoryUnavailable):
        save_bookmark(user=user, story_id=archived.pk)

    active, _ = make_story(6)
    empty = Story.objects.create(language="en")
    with pytest.raises(BookmarkStoryUnavailable):
        save_bookmark(user=user, story_id=empty.pk)
    saved = save_bookmark(user=user, story_id=active.pk)
    Story.objects.filter(pk=active.pk).update(status=Story.Status.ARCHIVED)
    assert save_bookmark(user=user, story_id=active.pk).saved_at == saved.saved_at
    remove_bookmark(user=user, story_id=active.pk)
    remove_bookmark(user=user, story_id=active.pk)
    assert not Bookmark.objects.filter(user=user, story=active).exists()


@pytest.mark.django_db(transaction=True)
def test_remove_is_user_scoped_and_resaving_gets_a_new_time(users, make_story):
    first_user, second_user = users
    story, _ = make_story(5)
    first = save_bookmark(user=first_user, story_id=story.pk)
    other = save_bookmark(user=second_user, story_id=story.pk)
    remove_bookmark(user=first_user, story_id=story.pk)
    assert Bookmark.objects.filter(user=second_user, story=story).exists()

    replacement = save_bookmark(user=first_user, story_id=story.pk)
    assert replacement.saved_at > first.saved_at
    assert replacement.saved_at >= other.saved_at


@pytest.mark.django_db(transaction=True)
def test_saved_page_order_tombstone_account_cursor_and_no_transfer(users, make_story):
    first_user, second_user = users
    stories = [make_story(number)[0] for number in range(10, 13)]
    rows = [Bookmark.objects.create(user=first_user, story=story) for story in stories]
    for offset, row in enumerate(rows):
        Bookmark.objects.filter(pk=row.pk).update(created_at=NOW + timedelta(minutes=offset))
    Story.objects.filter(pk=stories[-1].pk).update(status=Story.Status.ARCHIVED)
    replacement, _ = make_story(13)

    first = list_bookmarks(user=first_user, limit=2)
    assert [entry.story_id for entry in first.results] == [
        str(stories[2].pk),
        str(stories[1].pk),
    ]
    assert first.results[0].story is None
    assert first.results[1].story.content_state == "PREPARING"
    assert first.next_cursor
    second = list_bookmarks(user=first_user, cursor=first.next_cursor, limit=2)
    assert [entry.story_id for entry in second.results] == [str(stories[0].pk)]
    assert str(replacement.pk) not in [entry.story_id for entry in first.results + second.results]
    with pytest.raises(CursorError):
        list_bookmarks(user=second_user, cursor=first.next_cursor, limit=2)


@pytest.mark.django_db(transaction=True)
def test_batched_viewer_lookup_is_one_query_and_bounded(users, make_story):
    user, _ = users
    stories = [make_story(number)[0] for number in range(20, 23)]
    Bookmark.objects.create(user=user, story=stories[0])
    Bookmark.objects.create(user=user, story=stories[2])
    with CaptureQueriesContext(connection) as captured:
        result = bookmarked_story_ids(user=user, story_ids=[story.pk for story in stories])
    assert result == frozenset({stories[0].pk, stories[2].pk})
    assert len(captured) == 1
    with pytest.raises(ValueError):
        bookmarked_story_ids(user=user, story_ids=list(range(1, 52)))


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_saves_both_succeed_with_one_row(users, make_story):
    user, _ = users
    story, _ = make_story(30)
    barrier = threading.Barrier(2, timeout=20)

    def worker():
        close_old_connections()
        try:
            barrier.wait()
            return save_bookmark(user=user, story_id=story.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: worker(), range(2)))
    assert results[0].saved_at == results[1].saved_at
    assert Bookmark.objects.filter(user=user, story=story).count() == 1


@pytest.mark.django_db(transaction=True)
def test_archive_winning_story_lock_makes_save_a_controlled_unavailable_result(users, make_story):
    user, _ = users
    story, _ = make_story(31)
    locked = threading.Event()
    release = threading.Event()

    def archive():
        close_old_connections()
        try:
            with transaction.atomic():
                row = Story.objects.select_for_update().get(pk=story.pk)
                locked.set()
                assert release.wait(timeout=20)
                row.status = Story.Status.ARCHIVED
                row.save(update_fields=["status", "updated_at"])
        finally:
            close_old_connections()

    def save():
        close_old_connections()
        try:
            return save_bookmark(user=user, story_id=story.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(archive)
        assert locked.wait(timeout=20)
        save_future = executor.submit(save)
        release.set()
        future.result(timeout=20)
        with pytest.raises(BookmarkStoryUnavailable):
            save_future.result(timeout=20)
    assert not Bookmark.objects.filter(user=user, story=story).exists()


@pytest.mark.django_db(transaction=True)
def test_bookmark_operations_do_not_change_article_or_raw_provenance(users, make_story):
    user, _ = users
    story, article = make_story(40)
    before_articles = list(
        Article.objects.values_list("pk", "raw_article_id", "source_id", "endpoint_id")
    )
    before_raw = list(RawArticle.objects.values_list("pk", "endpoint_id", "payload_hash"))
    save_bookmark(user=user, story_id=story.pk)
    remove_bookmark(user=user, story_id=story.pk)
    assert (
        list(Article.objects.values_list("pk", "raw_article_id", "source_id", "endpoint_id"))
        == before_articles
    )
    assert list(RawArticle.objects.values_list("pk", "endpoint_id", "payload_hash")) == before_raw
    assert Article.objects.get(pk=article.pk).raw_article_id == article.raw_article_id


@pytest.mark.django_db(transaction=True)
def test_initial_migration_preserves_populated_accounts_news_and_provenance(make_story):
    executor = MigrationExecutor(connection)
    executor.migrate([("reading", None)])
    user = get_user_model().objects.create_user(username="migration-reader")
    story, article = make_story(60)
    before = {
        "user": (user.pk, user.username),
        "story": (story.pk, story.status),
        "article": (article.pk, article.raw_article_id),
        "raw": tuple(RawArticle.objects.values_list("pk", "payload_hash")),
    }

    executor = MigrationExecutor(connection)
    executor.migrate([("reading", "0001_initial")])

    assert (
        tuple(get_user_model().objects.filter(pk=user.pk).values_list("pk", "username").get())
        == before["user"]
    )
    assert (
        tuple(Story.objects.filter(pk=story.pk).values_list("pk", "status").get())
        == before["story"]
    )
    assert (
        tuple(Article.objects.filter(pk=article.pk).values_list("pk", "raw_article_id").get())
        == before["article"]
    )
    assert tuple(RawArticle.objects.values_list("pk", "payload_hash")) == before["raw"]
