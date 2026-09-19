"""Story structured logging (#33): shared context, step records and the allowlist.

The formatter is the one Django configures for the `pulso` tree. Content never
reaches a record by design, and the allowlist drops it even if a call site
tries: both are asserted here.
"""

import json
import logging
import pathlib

import pytest
from django.utils import timezone

from config.logging import SAFE_FIELDS, JsonLinesFormatter
from news import tasks
from news.application import embeddings as embeddings_module
from news.application.story_matching import match_article
from news.application.story_ports import SynthesisError, SynthesisErrorKind
from news.application.story_processing import process_article
from news.application.story_refresh import refresh_story
from news.logging import STEP_COMPLETED, STEP_FAILED, STORY_CONTEXT_FIELDS, story_logger
from news.models import Article, RawArticle, Source, SourceEndpoint, StoryArticle
from tests.news.recorded_embeddings import RecordedEmbeddingProvider
from tests.news.story_corpus import load_corpus

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SECRET = "SENTINEL_PUBLICATION_TEXT_DO_NOT_LOG"

#: The per-event fields #33 adds, each with a representative value.
STORY_FIELDS = {
    "candidate_count": 3,
    "chosen_story_id": 7,
    "distance": 0.071,
    "threshold": 0.18,
    "decision": "MATCH",
    "duration_ms": 12,
    "member_count": 4,
    "article_count": 4,
    "source_count": 3,
    "topic_count": 8,
    "entity_count": 5,
    "synthesis_source_count": 4,
    "refresh_state": "CURRENT",
    "refresh_reason": "membership_added",
    "error_kind": "INVALID_OUTPUT",
}


def story_records(log, **match):
    return [
        entry
        for entry in log.entries
        if entry["message"] in (STEP_COMPLETED, STEP_FAILED)
        and all(entry.get(key) == value for key, value in match.items())
    ]


@pytest.fixture
def recorded(monkeypatch):
    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: RecordedEmbeddingProvider()
    )


# --- the allowlist ----------------------------------------------------------------------


def format_record(**fields) -> dict:
    record = logging.LogRecord("pulso.news.stories", logging.INFO, "f.py", 1, "m", None, None)
    for key, value in fields.items():
        setattr(record, key, value)
    return json.loads(JsonLinesFormatter().format(record))


def test_every_story_field_and_context_field_is_allowlisted():
    assert set(STORY_FIELDS) <= set(SAFE_FIELDS)
    assert set(STORY_CONTEXT_FIELDS) <= set(SAFE_FIELDS)
    assert {"step", "failed_step", "match_reason", "match_rule", "matcher_key"} <= set(SAFE_FIELDS)
    assert len(SAFE_FIELDS) == len(set(SAFE_FIELDS)), "no field is listed twice"


def test_every_story_field_is_emitted_when_present():
    entry = format_record(**STORY_FIELDS, article_id=1, story_id=2, model_key="m", trigger="TASK")

    for field, value in STORY_FIELDS.items():
        assert entry[field] == value, field
    assert (entry["article_id"], entry["story_id"], entry["trigger"]) == (1, 2, "TASK")


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "description",
        "body_text",
        "payload",
        "text",
        "synthesis_text",
        "summary",
        "prompt",
        "system_prompt",
        "provider_response",
        "response",
        "vector",
        "embedding",
        "authorization",
        "credential",
        "secret",
        "error_message",
    ],
)
def test_content_fields_are_dropped_not_truncated(field, pulso_json_log):
    story_logger(article_id=5).info("attempted leak", extra={field: SECRET * 3})

    (line,) = pulso_json_log.lines
    entry = json.loads(line)
    assert field not in entry
    assert SECRET not in line
    assert SECRET[:20] not in line, "a dropped field must not survive as a truncated value"
    assert entry["article_id"] == 5


def test_content_field_names_are_absent_from_the_allowlist():
    forbidden = {
        "title",
        "description",
        "body_text",
        "payload",
        "text",
        "synthesis_text",
        "prompt",
        "provider_response",
        "vector",
        "embedding",
        "authorization",
        "credential",
        "secret",
    }
    assert forbidden.isdisjoint(SAFE_FIELDS)


# --- one Article's pipeline ------------------------------------------------------------


def make_article(number, title, body):
    source, _ = Source.objects.get_or_create(slug=f"s{number}", defaults={"name": f"S{number}"})
    endpoint = SourceEndpoint.objects.create(
        source=source,
        kind=SourceEndpoint.Kind.RSS,
        url=f"http://127.0.0.1/log-{number}.xml",
        is_active=False,
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"log-{number}",
        external_id=f"log-{number}",
        url=f"https://example.com/log/{number}",
        payload={"id": number},
        payload_hash=f"{number:064d}",
        fetched_at=timezone.now(),
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"log-{number}",
        canonical_url=f"https://example.com/log/{number}",
        title=title,
        description=f"{SECRET} description",
        body_text=f"{body} {SECRET}",
        language="en",
        content_fingerprint=f"{number:064x}",
        first_seen_at=timezone.now(),
    )


@pytest.mark.django_db
def test_one_articles_story_processing_shares_its_article_id(pulso_json_log):
    first = make_article(1, "Harbor closes after storm damage", "The harbor closed.")
    second = make_article(2, "Harbor closes after storm damage", "The harbor closed again.")
    process_article(first.pk)

    process_article(second.pk)

    records = story_records(pulso_json_log, article_id=second.pk)
    steps = [entry["step"] for entry in records]
    assert steps == [
        "article_embedding",
        "candidate_retrieval",
        "matching_decision",
        "story_association",
        "story_matching",
    ]
    model_key = records[0]["model_key"]
    for entry in records:
        assert entry["article_id"] == second.pk
        assert entry["model_key"] == model_key
        assert isinstance(entry["duration_ms"], int) and entry["duration_ms"] >= 0
    decision = records[2]
    story_id = StoryArticle.objects.get(article_id=second.pk).story_id
    assert decision["decision"] == "MATCH" and decision["match_reason"] == "WITHIN_THRESHOLD"
    assert decision["match_rule"] == "PRIMARY_DISTANCE"
    assert decision["chosen_story_id"] == story_id
    assert decision["threshold"] == 0.18 and decision["candidate_count"] == 1
    assert 0 <= decision["distance"] <= 0.18
    assert records[1]["candidate_count"] == 1
    assert records[3]["outcome"] == "MATCHED" and records[3]["story_id"] == story_id
    assert records[4]["state"] == "MATCHED" and records[4]["story_id"] == story_id
    # No other Article's id leaks into this pipeline's records.
    assert all(
        entry.get("article_id") in (None, first.pk, second.pk) for entry in pulso_json_log.entries
    )
    assert SECRET not in pulso_json_log.text()


@pytest.mark.django_db
def test_a_new_story_decision_logs_why(pulso_json_log):
    article = make_article(1, "Harbor closes after storm damage", "The harbor closed.")

    process_article(article.pk)

    decision = story_records(pulso_json_log, step="matching_decision")[0]
    assert decision["decision"] == "CREATE_NEW_STORY"
    assert decision["match_reason"] == "NO_CANDIDATES"
    assert decision["candidate_count"] == 0
    assert decision["chosen_story_id"] is None and decision["distance"] is None
    assert decision["match_rule"] is None


@pytest.mark.django_db
def test_the_secondary_decision_path_is_logged_without_content(recorded, pulso_json_log):
    loaded = load_corpus()
    process_article(loaded.article_ids["varrow-budget-01"])
    before = len(pulso_json_log.entries)

    process_article(loaded.article_ids["varrow-budget-02"])

    (decision,) = [
        entry
        for entry in pulso_json_log.entries[before:]
        if entry.get("step") == "matching_decision"
    ]
    story_id = StoryArticle.objects.get(article_id=loaded.article_ids["varrow-budget-01"]).story_id
    assert decision["decision"] == "MATCH"
    assert decision["match_reason"] == "VERIFIED_SAME_EVENT"
    assert decision["match_rule"] == "SECONDARY_EVENT_VERIFY"
    assert decision["chosen_story_id"] == story_id
    assert 0.18 < decision["distance"] <= 0.25 and decision["threshold"] == 0.18
    text = pulso_json_log.text().lower()
    for fixture_id in ("varrow-budget-01", "varrow-budget-02"):
        article = Article.objects.get(pk=loaded.article_ids[fixture_id])
        assert article.title.lower() not in text and article.body_text[:40].lower() not in text
    # The shared name itself is publication text: it is counted, never logged.
    assert "varrow" not in text


@pytest.mark.django_db
def test_an_already_assigned_article_logs_the_real_state_not_a_new_decision(pulso_json_log):
    article = make_article(1, "Harbor closes after storm damage", "The harbor closed.")
    process_article(article.pk)
    before = len(pulso_json_log.entries)

    match_article(article.pk)

    later = pulso_json_log.entries[before:]
    assert [entry["step"] for entry in later] == ["story_association"]
    assert later[0]["outcome"] == "ALREADY_ASSIGNED"


@pytest.mark.django_db
def test_task_records_carry_the_task_context(monkeypatch, pulso_json_log):
    article = make_article(1, "Harbor closes after storm damage", "The harbor closed.")
    queued = []
    monkeypatch.setattr(tasks.match_article_story, "delay", lambda *args: queued.append(args))

    tasks.embed_article_story.apply(args=[article.pk], task_id="embed-task-1")
    tasks.match_article_story.apply(args=[article.pk], task_id="match-task-1")

    embedding = story_records(pulso_json_log, step="article_embedding")[0]
    assert embedding["task_id"] == "embed-task-1"
    assert (embedding["attempt"], embedding["trigger"]) == (0, "TASK")
    for step in ("candidate_retrieval", "matching_decision", "story_association"):
        (entry,) = story_records(pulso_json_log, step=step)
        assert entry["task_id"] == "match-task-1" and entry["article_id"] == article.pk
        assert (entry["attempt"], entry["trigger"]) == (0, "TASK")
    assert queued == [(article.pk,)]


# --- one Story refresh -----------------------------------------------------------------


@pytest.mark.django_db
def test_a_refresh_emits_one_record_per_component_sharing_the_story_id(recorded, pulso_json_log):
    loaded = load_corpus()
    for fixture_id in ("harbor-storm-01", "harbor-storm-02"):
        process_article(loaded.article_ids[fixture_id])
    story_id = StoryArticle.objects.get(article_id=loaded.article_ids["harbor-storm-01"]).story_id
    before = len(pulso_json_log.entries)

    refresh_story(story_id, reason="membership_added")

    records = [
        entry
        for entry in pulso_json_log.entries[before:]
        if entry["message"] in (STEP_COMPLETED, STEP_FAILED)
    ]
    by_step = {entry["step"]: entry for entry in records}
    assert [entry["step"] for entry in records] == [
        "story_embedding",
        "topic_extraction",
        "entity_extraction",
        "story_synthesis",
        "story_refresh",
    ]
    assert all(entry["story_id"] == story_id for entry in records)
    assert all("article_id" not in entry for entry in records)
    assert by_step["story_embedding"]["member_count"] == 2
    assert by_step["story_embedding"]["model_key"] == RecordedEmbeddingProvider.identity.model_key
    assert by_step["topic_extraction"]["topic_count"] >= 1
    assert "entity_count" in by_step["entity_extraction"]
    synthesis = by_step["story_synthesis"]
    assert synthesis["synthesis_source_count"] == 2 and synthesis["element_count"] >= 1
    refresh = by_step["story_refresh"]
    assert refresh["outcome"] == "REFRESHED" and refresh["refresh_state"] == "CURRENT"
    assert refresh["refresh_reason"] == "membership_added"
    assert (refresh["member_count"], refresh["article_count"], refresh["source_count"]) == (2, 2, 2)
    for entry in records:
        assert isinstance(entry["duration_ms"], int) and entry["duration_ms"] >= 0
    for fixture_id in ("harbor-storm-01", "harbor-storm-02"):
        body = Article.objects.get(pk=loaded.article_ids[fixture_id]).body_text
        assert body[:40] not in pulso_json_log.text()


class BrokenSynthesizer:
    class identity:
        model_key = "stub:broken@1"

    def synthesize(self, prepared):
        raise SynthesisError(
            SynthesisErrorKind.SYNTHESIZER_FAILED, f"echo {SECRET}", model_key="stub:broken@1"
        )


@pytest.mark.django_db
def test_a_failed_refresh_names_the_failed_step_and_kind(recorded, pulso_json_log):
    loaded = load_corpus()
    process_article(loaded.article_ids["harbor-storm-01"])
    story_id = StoryArticle.objects.get(article_id=loaded.article_ids["harbor-storm-01"]).story_id

    refresh_story(story_id, synthesizer=BrokenSynthesizer())

    (failed,) = story_records(pulso_json_log, step="story_synthesis")
    assert failed["message"] == STEP_FAILED and failed["level"] == "WARNING"
    assert failed["error_kind"] == "SYNTHESIZER_FAILED"
    (refresh,) = story_records(pulso_json_log, step="story_refresh")
    assert refresh["outcome"] == "FAILED" and refresh["refresh_state"] == "FAILED"
    assert refresh["failed_step"] == "story_synthesis"
    assert refresh["error_kind"] == "SYNTHESIZER_FAILED"
    assert SECRET not in pulso_json_log.text()


@pytest.mark.django_db
def test_a_noop_refresh_states_no_refresh_state_of_its_own(recorded, pulso_json_log):
    loaded = load_corpus()
    process_article(loaded.article_ids["harbor-storm-01"])
    story_id = StoryArticle.objects.get(article_id=loaded.article_ids["harbor-storm-01"]).story_id
    refresh_story(story_id)
    before = len(pulso_json_log.entries)

    refresh_story(story_id)

    (refresh,) = [e for e in pulso_json_log.entries[before:] if e.get("step") == "story_refresh"]
    assert refresh["outcome"] == "NOOP" and "refresh_state" not in refresh
    assert [e.get("step") for e in pulso_json_log.entries[before:]] == ["story_refresh"]


# --- no external metrics platform -------------------------------------------------------


def test_no_metrics_or_tracing_dependency_is_added():
    for name in ("pyproject.toml", "uv.lock"):
        text = (BACKEND / name).read_text(encoding="utf-8").lower()
        for dependency in ("prometheus", "opentelemetry", "statsd", "datadog", "sentry"):
            assert dependency not in text, f"{dependency} in {name}"
