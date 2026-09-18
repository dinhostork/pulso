"""Story refresh generation and locking against real PostgreSQL."""

import threading
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from news.application import story_refresh
from news.application.embeddings import configured_provider
from news.application.story_candidates import find_candidates
from news.application.story_ports import EmbeddingError, EmbeddingErrorKind
from news.application.story_processing import reprocess_article
from news.application.story_refresh import RefreshOutcome, mark_story_stale, refresh_story
from news.domain.stories import member_signature
from news.models import (
    Article,
    IngestionRun,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
    StoryEntity,
    StorySynthesis,
    StoryTopic,
)
from news.tasks import refresh_story_task


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


def article(number, source=None):
    source = source or Source.objects.create(slug=f"source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": "Harbor closes"},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title="Harbor closes after storm",
        body_text="Port officials closed the harbor after a storm damaged two piers.",
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=timezone.now() + timedelta(minutes=number),
        first_seen_at=timezone.now(),
    )


def associate(story, member):
    with transaction.atomic():
        StoryArticle.objects.create(
            story=story, article=member, is_primary=True, method=StoryArticle.Method.MANUAL
        )
        mark_story_stale(story.pk, reason="membership_added")


def signatures(story):
    story.refresh_from_db()
    return (
        story.member_signature,
        list(StoryEmbedding.objects.filter(story=story).values_list("member_signature", flat=True)),
        list(StoryTopic.objects.filter(story=story).values_list("member_signature", flat=True)),
        list(StoryEntity.objects.filter(story=story).values_list("member_signature", flat=True)),
        list(
            StorySynthesis.objects.filter(story=story, is_current=True).values_list(
                "member_signature", flat=True
            )
        ),
    )


@pytest.mark.django_db(transaction=True)
def test_refresh_promotes_one_generation_counts_revisions_and_redelivery():
    story = Story.objects.create(language="en")
    first = article(1)
    second = article(2, first.source)
    third = article(3)
    for member in (first, second, third):
        associate(story, member)
    result = refresh_story(story.pk)
    assert result.outcome == RefreshOutcome.REFRESHED
    story.refresh_from_db()
    assert (story.article_count, story.source_count) == (3, 2)
    assert story.first_published_at == min(x.published_at for x in (first, second, third))
    assert story.last_published_at == max(x.published_at for x in (first, second, third))
    signature, *derived = signatures(story)
    assert all(value == signature for group in derived for value in group)
    assert derived[0] and derived[-1]
    old_refresh = story.refreshed_at
    assert refresh_story(story.pk).outcome == RefreshOutcome.NOOP
    story.refresh_from_db()
    assert story.refreshed_at == old_refresh
    assert StorySynthesis.objects.filter(story=story, is_current=True).count() == 1
    first.title = "Harbor reopens after inspection"
    with transaction.atomic():
        first.save(update_fields=["title", "updated_at"])
        mark_story_stale(story.pk, reason="article_revised")
    assert refresh_story(story.pk).outcome == RefreshOutcome.REFRESHED
    assert signatures(story)[0] != signature


@pytest.mark.django_db(transaction=True)
def test_zero_members_archive_and_keep_previous_generation():
    story = Story.objects.create(language="en")
    associate(story, article(10))
    refresh_story(story.pk)
    previous = StoryEmbedding.objects.get(story=story).member_signature
    with transaction.atomic():
        StoryArticle.objects.filter(story=story).delete()
        mark_story_stale(story.pk, reason="membership_removed")
    assert refresh_story(story.pk).outcome == RefreshOutcome.ARCHIVED
    story.refresh_from_db()
    assert (story.status, story.refresh_state, story.article_count, story.source_count) == (
        Story.Status.ARCHIVED,
        Story.RefreshState.CURRENT,
        0,
        0,
    )
    assert story.first_published_at is None and story.last_published_at is None
    assert story.member_signature == member_signature([])
    assert StoryEmbedding.objects.get(story=story).member_signature == previous


@pytest.mark.django_db(transaction=True)
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_mark_stale_dispatches_after_commit_and_rollback_does_not(monkeypatch):
    story = Story.objects.create(language="en")
    calls = []
    monkeypatch.setattr(story_refresh, "_dispatch", lambda *args: calls.append(args))
    with transaction.atomic():
        StoryArticle.objects.create(
            story=story, article=article(20), method=StoryArticle.Method.MANUAL
        )
        mark_story_stale(story.pk, reason="membership_added")
        assert calls == []
    assert calls == [(story.pk, "membership_added")]
    with pytest.raises(RuntimeError), transaction.atomic():
        mark_story_stale(story.pk, reason="rollback")
        raise RuntimeError
    assert calls == [(story.pk, "membership_added")]


class PausingProvider:
    def __init__(self, entered, resume):
        self.delegate = configured_provider()
        self.identity = self.delegate.identity
        self.entered = entered
        self.resume = resume

    def embed(self, texts):
        self.entered.set()
        assert self.resume.wait(10)
        return self.delegate.embed(texts)


@pytest.mark.django_db(transaction=True)
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_compute_holds_no_story_lock_and_discards_changed_snapshot(monkeypatch):
    story = Story.objects.create(language="en")
    associate(story, article(30))
    calls = []
    monkeypatch.setattr(story_refresh, "_dispatch", lambda *args: calls.append(args))
    entered, resume = threading.Event(), threading.Event()
    result = []

    def worker():
        close_old_connections()
        try:
            result.append(refresh_story(story.pk, provider=PausingProvider(entered, resume)))
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    with transaction.atomic():
        Story.objects.select_for_update(nowait=True).get(pk=story.pk)
        StoryArticle.objects.create(
            story=story, article=article(31), method=StoryArticle.Method.MANUAL
        )
        mark_story_stale(story.pk, reason="membership_added")
    resume.set()
    thread.join(10)
    assert not thread.is_alive()
    assert result[0].outcome == RefreshOutcome.STALE_RETRY
    assert not StoryEmbedding.objects.filter(story=story).exists()
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE
    assert (story.pk, "signature_changed") in calls
    assert refresh_story(story.pk).outcome == RefreshOutcome.REFRESHED
    assert all(value == signatures(story)[0] for group in signatures(story)[1:] for value in group)


@pytest.mark.django_db(transaction=True)
def test_failure_preserves_old_generation_and_can_be_rebuilt():
    story = Story.objects.create(language="en")
    associate(story, article(40))
    refresh_story(story.pk)
    previous = signatures(story)[0]
    associate(story, article(41))

    class FailingProvider:
        identity = configured_provider().identity

        def embed(self, _texts):
            raise EmbeddingError(
                EmbeddingErrorKind.PROVIDER_UNAVAILABLE, "Temporary outage", retryable=True
            )

    result = refresh_story(story.pk, provider=FailingProvider())
    assert result.outcome == RefreshOutcome.FAILED and result.retryable
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.FAILED
    assert story.member_signature == previous
    assert StoryEmbedding.objects.get(story=story).member_signature == previous
    assert refresh_story(story.pk).outcome == RefreshOutcome.REFRESHED


@pytest.mark.django_db(transaction=True)
def test_concurrent_refreshes_converge(monkeypatch):
    story = Story.objects.create(language="en")
    associate(story, article(50))
    results = []
    errors = []
    barrier = threading.Barrier(2)
    original = story_refresh.compute_story_embedding

    def overlap(*args):
        barrier.wait(10)
        return original(*args)

    monkeypatch.setattr(story_refresh, "compute_story_embedding", overlap)

    def worker():
        close_old_connections()
        try:
            results.append(refresh_story(story.pk))
        except Exception as error:
            errors.append(error)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert not errors
    assert {result.outcome for result in results} <= {RefreshOutcome.REFRESHED, RefreshOutcome.NOOP}
    assert StoryEmbedding.objects.filter(story=story).count() == 1
    assert StorySynthesis.objects.filter(story=story, is_current=True).count() == 1
    assert all(value == signatures(story)[0] for group in signatures(story)[1:] for value in group)


@pytest.mark.django_db(transaction=True)
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_failed_old_snapshot_cannot_replace_new_stale_state(monkeypatch):
    story = Story.objects.create(language="en")
    associate(story, article(60))
    entered, resume = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(story_refresh, "_dispatch", lambda *args: calls.append(args))

    class FailingAfterPause:
        identity = configured_provider().identity

        def embed(self, _texts):
            entered.set()
            assert resume.wait(10)
            raise EmbeddingError(
                EmbeddingErrorKind.PROVIDER_UNAVAILABLE, "Unavailable", retryable=True
            )

    outcomes = []

    def worker():
        close_old_connections()
        try:
            outcomes.append(refresh_story(story.pk, provider=FailingAfterPause()))
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    associate(story, article(61))
    resume.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0].outcome == RefreshOutcome.STALE_RETRY
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE
    assert story.refresh_error == ""
    assert (story.pk, "signature_changed") in calls


@pytest.mark.django_db(transaction=True)
def test_reader_sees_previous_generation_during_partial_uncommitted_promotion(monkeypatch):
    story = Story.objects.create(language="en")
    associate(story, article(70))
    refresh_story(story.pk)
    previous = signatures(story)[0]
    associate(story, article(71))
    entered, resume = threading.Event(), threading.Event()
    original = story_refresh.persist_story_enrichment

    def pause_promotion(*args):
        entered.set()
        assert resume.wait(10)
        return original(*args)

    monkeypatch.setattr(story_refresh, "persist_story_enrichment", pause_promotion)
    outcomes = []

    def worker():
        close_old_connections()
        try:
            outcomes.append(refresh_story(story.pk))
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(10)
    for _ in range(10):
        with transaction.atomic():
            old = signatures(story)
            assert old[0] == previous
            assert all(value == previous for group in old[1:] for value in group)
    resume.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0].outcome == RefreshOutcome.REFRESHED
    new = signatures(story)
    assert new[0] != previous
    assert all(value == new[0] for group in new[1:] for value in group)


@pytest.mark.django_db(transaction=True)
def test_deleting_complete_derived_set_rebuilds_it():
    story = Story.objects.create(language="en")
    associate(story, article(80))
    refresh_story(story.pk)
    StoryEmbedding.objects.filter(story=story).delete()
    StoryTopic.objects.filter(story=story).delete()
    StoryEntity.objects.filter(story=story).delete()
    StorySynthesis.objects.filter(story=story).delete()
    assert refresh_story(story.pk).outcome == RefreshOutcome.REFRESHED
    signature, *derived = signatures(story)
    assert derived[0] and derived[-1]
    assert all(value == signature for group in derived for value in group)


@pytest.mark.django_db(transaction=True)
def test_current_same_signature_is_read_only_and_calls_no_component(monkeypatch):
    story = Story.objects.create(language="en")
    associate(story, article(90))
    refresh_story(story.pk)
    story.refresh_from_db()
    refreshed_at = story.refreshed_at

    def unexpected(*_args, **_kwargs):
        raise AssertionError("component called on no-op")

    monkeypatch.setattr(story_refresh, "compute_story_embedding", unexpected)
    monkeypatch.setattr(story_refresh, "compute_story_enrichment", unexpected)
    monkeypatch.setattr(story_refresh, "compute_story_synthesis", unexpected)
    with CaptureQueriesContext(connection) as queries:
        assert refresh_story(story.pk).outcome == RefreshOutcome.NOOP
    assert not any(
        query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
        for query in queries
    )
    story.refresh_from_db()
    assert story.refreshed_at == refreshed_at


@pytest.mark.django_db(transaction=True)
def test_refresh_never_changes_news_provenance_columns():
    story = Story.objects.create(language="en")
    member = article(100)
    IngestionRun.objects.create(endpoint=member.endpoint, trigger="test", started_at=timezone.now())
    associate(story, member)
    models = (Article, RawArticle, IngestionRun, Source, SourceEndpoint)
    before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
    assert refresh_story(story.pk).outcome == RefreshOutcome.REFRESHED
    after = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
    assert after == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "component", ["compute_story_embedding", "compute_story_enrichment", "compute_story_synthesis"]
)
def test_each_component_failure_keeps_previous_generation(monkeypatch, component):
    story = Story.objects.create(language="en")
    associate(story, article(110))
    refresh_story(story.pk)
    old_signature, *old_derived = signatures(story)
    associate(story, article(111))

    def fail(*_args, **_kwargs):
        raise RuntimeError("secret publication text")

    monkeypatch.setattr(story_refresh, component, fail)
    result = refresh_story(story.pk)
    assert result.outcome == RefreshOutcome.FAILED
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.FAILED
    assert story.refresh_error == "RuntimeError"
    assert signatures(story) == (old_signature, *old_derived)


@pytest.mark.django_db(transaction=True)
def test_reprocessing_last_member_stales_and_archives_old_story():
    story = Story.objects.create(language="en")
    member = article(120)
    associate(story, member)
    refresh_story(story.pk)
    previous = StoryEmbedding.objects.get(story=story).member_signature
    result = reprocess_article(member.pk)
    assert result.story_id != story.pk
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE
    assert refresh_story(story.pk).outcome == RefreshOutcome.ARCHIVED
    story.refresh_from_db()
    assert story.status == Story.Status.ARCHIVED
    assert StoryEmbedding.objects.get(story=story).member_signature == previous
    assert story.pk not in {
        candidate.story_id
        for candidate in find_candidates(member.pk, configured_provider().identity.model_key)
    }


@pytest.mark.django_db(transaction=True)
def test_refresh_task_redelivery_is_a_noop_without_duplicate_rows():
    story = Story.objects.create(language="en")
    associate(story, article(130))
    first = refresh_story_task.apply(args=(story.pk,), kwargs={"reason": "redelivery"}).get()
    second = refresh_story_task.apply(args=(story.pk,), kwargs={"reason": "redelivery"}).get()
    assert (first["outcome"], second["outcome"]) == ("REFRESHED", "NOOP")
    assert StoryEmbedding.objects.filter(story=story).count() == 1
    assert StorySynthesis.objects.filter(story=story, is_current=True).count() == 1
    assert (
        StoryTopic.objects.filter(story=story).count()
        == StoryTopic.objects.filter(story=story).values("topic_id").distinct().count()
    )
    assert (
        StoryEntity.objects.filter(story=story).count()
        == StoryEntity.objects.filter(story=story).values("entity_id").distinct().count()
    )


@pytest.mark.django_db(transaction=True)
def test_failed_story_is_reachable_from_bounded_operator_sweep():
    story = Story.objects.create(language="en")
    associate(story, article(140))
    Story.objects.filter(pk=story.pk).update(refresh_state=Story.RefreshState.FAILED)
    output = StringIO()
    call_command("news_story_refresh", stale_failed=True, limit=1, stdout=output)
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.CURRENT
    assert f"story_id={story.pk} outcome=REFRESHED" in output.getvalue()
