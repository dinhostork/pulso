"""Story processing orchestration: state, freshness, retries and isolation (#29).

Embedding uses the offline deterministic provider from the test settings.
Celery tasks run eagerly through `.apply()`; dispatches are recorded, never
sent to a broker.
"""

import ast
import inspect
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from news import tasks
from news.adapters.deterministic_embeddings import DeterministicEmbeddingProvider
from news.application import story_matching as matching_module
from news.application import story_processing as processing_module
from news.application.ingest import ingest_endpoint
from news.application.ports import FetchedItem, FetchResult
from news.application.process import process_raw_article
from news.application.story_candidates import find_candidates
from news.application.story_ports import EmbeddingError, EmbeddingErrorKind, EmbeddingModel
from news.application.story_processing import (
    PipelineKeys,
    claim_for_reconciliation,
    current_keys,
    embed_step,
    match_step,
    process_article,
    reconciliation_candidates,
    reprocess_article,
)
from news.domain.fingerprints import payload_hash
from news.models import (
    Article,
    ArticleEmbedding,
    ArticleStoryProcessing,
    IngestionRun,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
)

State = ArticleStoryProcessing.State
SECRET = "DO_NOT_LEAK_THIS_ARTICLE_TEXT"
STORY_TASKS = (tasks.embed_article_story, tasks.match_article_story)


class FakeFetcher:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))
    monkeypatch.setattr("news.application.ingest.Fetcher", FakeFetcher)
    monkeypatch.setattr(tasks, "_jitter", lambda _spread: 0.0)


@pytest.fixture
def dispatched(monkeypatch):
    """Story task dispatches, recorded instead of sent to a broker."""

    calls = []
    for task in (*STORY_TASKS,):
        monkeypatch.setattr(
            task, "delay", lambda *args, _name=task.name, **kwargs: calls.append((_name, args))
        )
    return calls


_counter = iter(range(1, 100_000))


def make_article(title="Harbor closes after storm damage", body=""):
    number = next(_counter)
    source = Source.objects.create(slug=f"source-{number}", name=f"Source {number}")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed.xml"
    )
    run = IngestionRun.objects.create(endpoint=endpoint, trigger="test", started_at=timezone.now())
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        ingestion_run=run,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": title},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=title,
        body_text=body,
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=timezone.now(),
        first_seen_at=timezone.now(),
    )


def provenance():
    return [
        list(model.objects.order_by("pk").values())
        for model in (Article, RawArticle, IngestionRun, Source, SourceEndpoint)
    ]


def derived_counts():
    return [
        model.objects.count()
        for model in (Story, StoryArticle, StoryEmbedding, ArticleEmbedding, ArticleStoryProcessing)
    ]


class FailingProvider:
    identity = DeterministicEmbeddingProvider.identity

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        raise self.error


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(processing_module, "configured_provider", lambda: provider)


def change_matcher_key(monkeypatch, key="story-match-v2;changed"):
    monkeypatch.setattr(processing_module, "MATCHER_KEY", key)
    monkeypatch.setattr(matching_module, "MATCHER_KEY", key)


def change_embedding_model(monkeypatch):
    class OtherModel(DeterministicEmbeddingProvider):
        identity = EmbeddingModel("deterministic", "sha256-token-hash", "2", 64)

    use_provider(monkeypatch, OtherModel())


def age_rows():
    ArticleStoryProcessing.objects.update(updated_at=timezone.now() - timedelta(hours=1))


def story_log(caplog, message):
    return [record for record in caplog.records if record.message == message]


# --- the pipeline -------------------------------------------------------------


@pytest.mark.django_db
def test_embed_then_match_records_both_keys_and_the_story():
    article = make_article()

    embedded = embed_step(article.pk)
    matched = match_step(article.pk)

    row = ArticleStoryProcessing.objects.get(article=article)
    keys = current_keys()
    assert embedded.state == State.EMBEDDED
    assert matched.state == State.MATCHED == row.state
    assert (row.embedding_model_key, row.matcher_key) == (
        keys.embedding_model_key,
        keys.matcher_key,
    )
    assert (
        matched.pipeline_key
        == keys.pipeline_key
        == (f"{keys.embedding_model_key}|{keys.matcher_key}")
    )
    assert matched.story_id == StoryArticle.objects.get(article=article).story_id
    assert (row.attempts, row.error_kind, row.error_message) == (0, "", "")


@pytest.mark.django_db
def test_both_keys_are_required_on_every_matched_row():
    for _ in range(3):
        process_article(make_article().pk)

    assert (
        not ArticleStoryProcessing.objects.filter(state=State.MATCHED)
        .filter(embedding_model_key="")
        .exists()
    )
    assert not ArticleStoryProcessing.objects.filter(state=State.MATCHED, matcher_key="").exists()
    row = ArticleStoryProcessing.objects.first()
    for blank in ({"embedding_model_key": ""}, {"matcher_key": ""}):
        with pytest.raises(IntegrityError), transaction.atomic():
            ArticleStoryProcessing.objects.filter(pk=row.pk).update(**blank)
    ArticleStoryProcessing.objects.filter(pk=row.pk).update(state=State.FAILED, matcher_key="")


def test_pipeline_key_composes_both_halves_unambiguously():
    keys = PipelineKeys("fastembed:BAAI/bge-small-en-v1.5@52398278842e", "story-match-v1;x=1")
    assert keys.pipeline_key == "fastembed:BAAI/bge-small-en-v1.5@52398278842e|story-match-v1;x=1"
    assert PipelineKeys("a", "b").pipeline_key != PipelineKeys("a", "c").pipeline_key
    assert PipelineKeys("a", "b").pipeline_key != PipelineKeys("x", "b").pipeline_key


@pytest.mark.django_db
def test_redelivered_task_messages_create_no_additional_rows(dispatched):
    article = make_article()
    tasks.embed_article_story.apply(args=[article.pk])
    tasks.match_article_story.apply(args=[article.pk])
    counts = derived_counts()

    for _ in range(2):
        tasks.embed_article_story.apply(args=[article.pk])
        tasks.match_article_story.apply(args=[article.pk])

    assert derived_counts() == counts
    assert counts == [1, 1, 1, 1, 1]
    # A fresh Article is not handed to matching again.
    assert dispatched == [(tasks.match_article_story.name, (article.pk,))]


@pytest.mark.django_db
def test_reprocessing_rebuilds_a_deleted_association_without_touching_provenance():
    article = make_article(body=SECRET)
    process_article(article.pk)
    before = provenance()
    StoryArticle.objects.filter(article=article).delete()

    rebuilt = process_article(article.pk)

    assert rebuilt.state == State.MATCHED
    assert StoryArticle.objects.filter(article=article, is_primary=True).exists()
    reprocessed = reprocess_article(article.pk)
    assert reprocessed.state == State.MATCHED
    assert StoryArticle.objects.filter(article=article, is_primary=True).count() == 1
    assert ArticleEmbedding.objects.filter(article=article).count() == 1
    assert provenance() == before


@pytest.mark.django_db
def test_matcher_change_replaces_the_stale_association(monkeypatch):
    article = make_article()
    first = process_article(article.pk)
    change_matcher_key(monkeypatch)

    second = process_article(article.pk)

    association = StoryArticle.objects.get(article=article, is_primary=True)
    assert association.matcher_key == "story-match-v2;changed"
    assert second.pipeline_key.endswith("|story-match-v2;changed")
    assert second.story_id != first.story_id
    # The Story left without members is not archived here (#32 owns that),
    # and it can never be a retrieval candidate.
    emptied = Story.objects.get(pk=first.story_id)
    assert emptied.status == Story.Status.ACTIVE
    assert not StoryArticle.objects.filter(story=emptied).exists()
    other = make_article()
    embed_step(other.pk)
    candidates = find_candidates(other.pk, current_keys().embedding_model_key, max_distance=2.0)
    assert emptied.pk not in [candidate.story_id for candidate in candidates]


# --- failures and retries ------------------------------------------------------


@pytest.mark.django_db
def test_transient_provider_failure_retries_on_a_bounded_schedule(monkeypatch, caplog, dispatched):
    article = make_article(body=SECRET)
    provider = FailingProvider(TimeoutError(f"provider timed out on {SECRET}"))
    use_provider(monkeypatch, provider)

    result = tasks.embed_article_story.apply(args=[article.pk])

    assert provider.calls == 4  # one execution plus max_retries
    records = story_log(caplog, "News Story embedding failed")
    assert [record.will_retry for record in records] == [True, True, True, False]
    assert [record.countdown for record in records] == [30, 60, 120, None]
    row = ArticleStoryProcessing.objects.get(article=article)
    assert row.state == State.FAILED
    assert row.error_kind == EmbeddingErrorKind.PROVIDER_UNAVAILABLE
    assert row.attempts == 4
    assert SECRET not in row.error_message
    assert result.result["state"] == State.FAILED
    assert dispatched == []


@pytest.mark.django_db
def test_database_outage_during_matching_is_transient(monkeypatch, caplog, dispatched):
    article = make_article()
    embed_step(article.pk)

    def unavailable(*_args, **_kwargs):
        raise OperationalError("connection lost")

    monkeypatch.setattr(processing_module, "match_article", unavailable)
    tasks.match_article_story.apply(args=[article.pk])

    records = story_log(caplog, "News Story matching failed")
    assert [record.will_retry for record in records] == [True, True, True, False]
    row = ArticleStoryProcessing.objects.get(article=article)
    assert (row.state, row.error_kind, row.attempts) == (State.FAILED, "DATABASE_UNAVAILABLE", 4)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind",
    [
        EmbeddingErrorKind.DIMENSION_MISMATCH,
        EmbeddingErrorKind.PROVIDER_FAILED,
        EmbeddingErrorKind.INVALID_OUTPUT,
    ],
)
def test_permanent_failure_is_recorded_once_and_never_retried(
    monkeypatch, caplog, dispatched, kind
):
    article = make_article(body=SECRET)
    provider = FailingProvider(EmbeddingError(kind, "Safe provider summary. " * 60))
    use_provider(monkeypatch, provider)

    tasks.embed_article_story.apply(args=[article.pk])

    assert provider.calls == 1
    records = story_log(caplog, "News Story embedding failed")
    assert [record.will_retry for record in records] == [False]
    row = ArticleStoryProcessing.objects.get(article=article)
    assert (row.state, row.error_kind, row.attempts) == (State.FAILED, kind, 1)
    assert 0 < len(row.error_message) <= 512
    assert SECRET not in row.error_message
    assert dispatched == []


@pytest.mark.django_db
def test_missing_embedding_at_matching_is_permanent(dispatched):
    article = make_article()

    result = tasks.match_article_story.apply(args=[article.pk]).result

    assert result["state"] == State.FAILED
    assert result["error_kind"] == "MISSING_EMBEDDING"
    assert not Story.objects.exists()


@pytest.mark.django_db
def test_unexpected_errors_are_recorded_by_class_and_raised(monkeypatch):
    article = make_article()

    def broken(*_args, **_kwargs):
        raise KeyError(SECRET)

    monkeypatch.setattr(processing_module, "embed_article", broken)
    with pytest.raises(KeyError):
        embed_step(article.pk)

    row = ArticleStoryProcessing.objects.get(article=article)
    assert (row.state, row.error_kind, row.error_message) == (
        State.FAILED,
        "UNEXPECTED",
        "KeyError",
    )


@pytest.mark.django_db
def test_attempt_cap_stops_reconciliation_but_keeps_the_failure_visible(monkeypatch):
    article = make_article()
    use_provider(
        monkeypatch,
        FailingProvider(EmbeddingError(EmbeddingErrorKind.INVALID_INPUT, "Unusable input.")),
    )

    with override_settings(NEWS_STORY_PROCESSING_MAX_ATTEMPTS=3):
        for expected_attempts in (1, 2, 3):
            age_rows()
            assert reconciliation_candidates() == [article.pk]
            embed_step(article.pk)
            row = ArticleStoryProcessing.objects.get(article=article)
            assert (row.state, row.attempts) == (State.FAILED, expected_attempts)
        age_rows()
        assert reconciliation_candidates() == []

    assert ArticleStoryProcessing.objects.get(article=article).state == State.FAILED


@pytest.mark.django_db
def test_missing_article_is_skipped_without_retry(dispatched):
    assert tasks.embed_article_story.apply(args=[987654]).result == {
        "article_id": 987654,
        "state": "MISSING",
    }


# --- reconciliation ------------------------------------------------------------


@pytest.mark.django_db
def test_fully_processed_database_reconciles_to_nothing(dispatched):
    for _ in range(3):
        process_article(make_article().pk)
    age_rows()

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = tasks.reconcile_article_stories.apply().result

    assert result == {"dispatched": 0}
    assert dispatched == []


@pytest.mark.django_db
def test_reconciliation_dispatches_a_bounded_batch_in_article_order(dispatched):
    articles = [make_article() for _ in range(5)]

    with override_settings(NEWS_STORY_RECONCILE_BATCH=3):
        with TestCase.captureOnCommitCallbacks(execute=True) as callbacks:
            result = tasks.reconcile_article_stories.apply().result
            assert dispatched == []  # nothing is sent before the selection commits

    first_three = [article.pk for article in articles[:3]]
    assert result == {"dispatched": 3} and len(callbacks) == 3
    assert dispatched == [(tasks.embed_article_story.name, (pk,)) for pk in first_three]
    # Claimed rows are left alone until the cutoff passes again.
    assert set(
        ArticleStoryProcessing.objects.filter(state=State.PENDING).values_list(
            "article_id", flat=True
        )
    ) == set(first_three)
    assert reconciliation_candidates() == [article.pk for article in articles[3:]]


@pytest.mark.django_db
def test_matcher_key_change_alone_makes_processed_articles_stale(monkeypatch):
    articles = [make_article() for _ in range(3)]
    for article in articles:
        process_article(article.pk)
    age_rows()
    assert reconciliation_candidates() == []

    change_matcher_key(monkeypatch)

    assert current_keys().embedding_model_key == DeterministicEmbeddingProvider.identity.model_key
    assert reconciliation_candidates() == [article.pk for article in articles]


@pytest.mark.django_db
def test_embedding_model_change_alone_makes_processed_articles_stale(monkeypatch):
    articles = [make_article() for _ in range(3)]
    for article in articles:
        process_article(article.pk)
    age_rows()
    assert reconciliation_candidates() == []
    unchanged_matcher = current_keys().matcher_key

    change_embedding_model(monkeypatch)

    assert current_keys().matcher_key == unchanged_matcher
    assert reconciliation_candidates() == [article.pk for article in articles]


@pytest.mark.django_db
def test_neither_key_alone_decides_freshness(monkeypatch):
    matcher_stale, embedding_stale, fresh = (make_article() for _ in range(3))
    for article in (matcher_stale, embedding_stale, fresh):
        process_article(article.pk)
    keys = current_keys()
    ArticleStoryProcessing.objects.filter(article=matcher_stale).update(matcher_key="old-policy")
    ArticleStoryProcessing.objects.filter(article=embedding_stale).update(
        embedding_model_key="old-model"
    )
    age_rows()

    assert reconciliation_candidates() == [matcher_stale.pk, embedding_stale.pk]
    assert ArticleStoryProcessing.objects.get(article=matcher_stale).embedding_model_key == (
        keys.embedding_model_key
    )
    assert ArticleStoryProcessing.objects.get(article=embedding_stale).matcher_key == (
        keys.matcher_key
    )


@pytest.mark.django_db
def test_recent_and_in_flight_rows_wait_for_the_cutoff():
    pending, embedded, matched_without_story = (make_article() for _ in range(3))
    claim_for_reconciliation([pending.pk])
    embed_step(embedded.pk)
    process_article(matched_without_story.pk)
    StoryArticle.objects.filter(article=matched_without_story).delete()

    assert reconciliation_candidates() == []
    age_rows()
    assert reconciliation_candidates() == [pending.pk, embedded.pk, matched_without_story.pk]


# --- News Core isolation ---------------------------------------------------------


def fetched(identity):
    return FetchedItem(
        external_id=identity,
        url=f"https://news.example/{identity}",
        title=f"Story task isolation {identity}",
        content_html=f"<p>{SECRET}</p>",
    )


@pytest.mark.django_db
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_failing_story_dispatch_leaves_the_ingestion_result_unchanged(monkeypatch):
    source = Source.objects.create(slug="isolation", name="Isolation")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind="RSS", url="https://feed.example/rss"
    )

    class Adapter:
        kind = "RSS"

        def fetch(self, request, fetcher):
            return FetchResult(items=(fetched("one"), fetched("two")))

    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: Adapter())
    attempts = []

    def raising(article_id):
        attempts.append(article_id)
        raise RuntimeError("broker down")

    monkeypatch.setattr(tasks.embed_article_story, "delay", raising)

    with TestCase.captureOnCommitCallbacks(execute=True) as callbacks:
        summary = ingest_endpoint(endpoint.pk, trigger="MANUAL")

    run = IngestionRun.objects.get(pk=summary.run_id)
    assert summary.status == run.status == IngestionRun.Status.SUCCEEDED
    assert (run.items_received, run.raw_created, run.items_processed, run.items_failed) == (
        2,
        2,
        2,
        0,
    )
    articles = list(Article.objects.filter(source=source).order_by("pk"))
    assert len(articles) == 2
    assert sorted(attempts) == [article.pk for article in articles] and len(callbacks) == 2
    assert set(RawArticle.objects.values_list("outcome", flat=True)) == {"ARTICLE_CREATED"}
    assert not ArticleStoryProcessing.objects.exists()


def pending_raw(identity):
    source = Source.objects.create(slug=f"raw-{identity}", name=identity)
    endpoint = SourceEndpoint.objects.create(
        source=source, kind="RSS", url=f"https://{identity}.example/rss"
    )
    payload = {
        "external_id": identity,
        "url": f"https://news.example/{identity}",
        "title": "Dispatch check",
        "summary_html": None,
        "content_html": "<p>Body.</p>",
        "published_at": None,
        "updated_at": None,
        "authors": [],
        "language": "en",
        "raw": {},
        "truncated": False,
    }
    return RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=identity,
        external_id=identity,
        url=payload["url"],
        payload=payload,
        payload_hash=payload_hash(payload),
        fetched_at=timezone.now(),
    )


class Rollback(Exception):
    pass


@pytest.mark.django_db
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_dispatch_waits_for_commit_and_a_rollback_sends_nothing(dispatched):
    rolled_back = pending_raw("rolled-back")
    committed = pending_raw("committed")

    with TestCase.captureOnCommitCallbacks(execute=True) as callbacks:
        with pytest.raises(Rollback), transaction.atomic():
            process_raw_article(rolled_back.pk)
            raise Rollback
    assert callbacks == [] and dispatched == []

    with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
        outcome = process_raw_article(committed.pk)
        assert dispatched == []
    for callback in callbacks:
        callback()
    assert dispatched == [(tasks.embed_article_story.name, (outcome.article_id,))]


@pytest.mark.django_db
def test_nothing_is_dispatched_while_story_processing_is_disabled(dispatched):
    with TestCase.captureOnCommitCallbacks(execute=True) as callbacks:
        process_raw_article(pending_raw("disabled").pk)
    assert callbacks == [] and dispatched == []


# --- task shape ---------------------------------------------------------------


def test_story_task_arguments_are_primitives():
    for task in STORY_TASKS:
        parameters = list(inspect.signature(task.run).parameters.values())
        assert [parameter.name for parameter in parameters] == ["article_id"]
        assert parameters[0].annotation is int
    assert list(inspect.signature(tasks.reconcile_article_stories.run).parameters) == []


def test_task_module_owns_no_story_rules():
    tree = ast.parse(Path(tasks.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("news.domain") for module in imported)
    assert not imported & {
        "news.application.embeddings",
        "news.application.story_candidates",
        "news.application.story_matching",
        "news.application.story_ports",
    }
    assert "news.application.story_processing" in imported


@pytest.mark.django_db
def test_story_task_payloads_are_identifiers_and_states(dispatched):
    article = make_article(body=SECRET)
    payload = tasks.embed_article_story.apply(args=[article.pk]).result

    assert set(payload) == {
        "article_id",
        "state",
        "pipeline_key",
        "attempts",
        "error_kind",
        "story_id",
    }
    assert all(isinstance(value, (int, str, type(None))) for value in payload.values())
    assert SECRET not in str(payload)


# --- operator commands ------------------------------------------------------------


@pytest.mark.django_db
def test_process_and_reprocess_commands_print_identifiers_only():
    article = make_article(body=SECRET)
    out = StringIO()

    call_command("news_story_process", "--article", str(article.pk), stdout=out)
    call_command("news_story_process", "--article", str(article.pk), "--reprocess", stdout=out)

    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert all(f"article_id={article.pk} state=MATCHED" in line for line in lines)
    assert all(f"pipeline_key={current_keys().pipeline_key}" in line for line in lines)
    assert SECRET not in out.getvalue() and article.title not in out.getvalue()
    with pytest.raises(CommandError, match="No Article"):
        call_command("news_story_process", "--article", "999999")


@pytest.mark.django_db
def test_reconcile_command_processes_a_bounded_batch():
    articles = [make_article() for _ in range(4)]
    out = StringIO()

    call_command("news_story_reconcile", "--limit", "2", stdout=out)

    lines = out.getvalue().splitlines()
    assert lines[0].startswith("selected=2 pipeline_key=")
    assert [line.split()[0] for line in lines[1:]] == [f"article_id={a.pk}" for a in articles[:2]]
    assert ArticleStoryProcessing.objects.filter(state=State.MATCHED).count() == 2
    with pytest.raises(CommandError):
        call_command("news_story_reconcile", "--limit", "0")
