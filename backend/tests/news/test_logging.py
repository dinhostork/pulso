"""Structured-logging contract and hygiene for News ingestion (issue #20).

The formatter under test is the one Django actually configures for the `pulso`
tree, not a test-only stand-in.
"""

import importlib.util
import json
import logging
import os
import pathlib
from unittest import mock

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.logging import SAFE_FIELDS, JsonLinesFormatter
from diagnostics.application import execute_diagnostic_ping
from news.application.ingest import ingest_endpoint
from news.application.ports import FetchError, FetchErrorKind, FetchResponse
from news.application.process import process_raw_article
from news.domain.fingerprints import payload_hash
from news.logging import INGESTION_CONTEXT_FIELDS, ingestion_logger
from news.models import RawArticle, Source, SourceEndpoint
from news.tasks import poll_due_endpoints

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "news"
COMMON_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "common.py"

# Values that must never reach a log line, in any field, at any level.
CONFIG_SENTINEL = "SENTINEL_ADAPTER_CONFIG_VALUE"
HEADER_SENTINEL = "SENTINEL_RESPONSE_HEADER"
PAYLOAD_SENTINEL = "SENTINEL_RAW_PAYLOAD"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="observed", name="Observed")
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


def ingest_fixture(endpoint, monkeypatch, name, *, content_type="application/rss+xml"):
    body = (FIXTURES / name).read_bytes()

    class FixtureFetcher:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, *_args, **_kwargs):
            return FetchResponse(body=body, content_type=content_type)

    monkeypatch.setattr("news.application.ingest.Fetcher", FixtureFetcher)
    return ingest_endpoint(endpoint.pk, trigger="SCHEDULE")


# --- formatter contract ------------------------------------------------------


def test_configured_pulso_logger_uses_the_json_formatter(pulso_formatter):
    logger = logging.getLogger("pulso")

    assert isinstance(pulso_formatter, JsonLinesFormatter)
    # Structured records are not duplicated as plain text through the root.
    assert logger.propagate is False
    assert logger.level == getattr(logging, settings.LOG_LEVEL)


def test_django_and_celery_loggers_are_untouched():
    assert logging.getLogger("django").propagate is True
    assert logging.getLogger("celery").propagate is True
    assert "django" not in settings.LOGGING["loggers"]
    assert "celery" not in settings.LOGGING["loggers"]
    assert set(settings.LOGGING["loggers"]) == {"pulso"}


def format_record(**fields) -> dict:
    record = logging.LogRecord(
        "pulso.news.ingest", logging.INFO, "file.py", 1, "A message", None, None
    )
    for key, value in fields.items():
        setattr(record, key, value)
    line = JsonLinesFormatter().format(record)
    assert "\n" not in line, "one record must render as one line"
    return json.loads(line)


def test_base_fields_are_always_present():
    entry = format_record()

    assert entry["level"] == "INFO"
    assert entry["logger"] == "pulso.news.ingest"
    assert entry["message"] == "A message"
    assert entry["timestamp"].endswith("+00:00")


def test_parameterized_messages_keep_working():
    record = logging.LogRecord(
        "pulso.news.adapters.rss",
        logging.WARNING,
        "file.py",
        1,
        "adapter=%s entries=%d",
        ("RSS", 10),
        None,
    )

    entry = json.loads(JsonLinesFormatter().format(record))

    assert entry["message"] == "adapter=RSS entries=10"


def test_allowlisted_operational_fields_are_emitted():
    entry = format_record(run_id=7, endpoint_id=2, adapter="RSS", items_received=10)

    assert entry["run_id"] == 7 and entry["endpoint_id"] == 2
    assert entry["adapter"] == "RSS" and entry["items_received"] == 10


@pytest.mark.parametrize(
    "field",
    ["payload", "title", "description", "body_text", "content_html", "adapter_config", "headers"],
)
def test_a_non_allowlisted_field_is_never_serialized(field):
    """Guards future call sites: the formatter is not a generic extra dumper."""

    entry = format_record(**{field: "TOP_SECRET_BODY"})

    assert field not in entry
    assert "TOP_SECRET_BODY" not in json.dumps(entry)


def test_content_field_names_are_absent_from_the_allowlist():
    forbidden = {
        "payload",
        "title",
        "description",
        "body_text",
        "summary_html",
        "content_html",
        "adapter_config",
        "headers",
        "authorization",
        "cookies",
        "error_message",
    }

    assert forbidden.isdisjoint(SAFE_FIELDS)


def test_non_primitive_values_are_described_by_type_not_content():
    entry = format_record(outcome=Source(slug="leak", name="Leaky name"))

    assert entry["outcome"] == "Source"
    assert "Leaky name" not in json.dumps(entry)


def test_long_values_are_bounded():
    entry = format_record(canonical_url="https://news.example/" + "x" * 5000)

    assert len(entry["canonical_url"]) == 512


def test_exception_info_yields_only_the_class():
    try:
        raise ValueError(PAYLOAD_SENTINEL)
    except ValueError:
        record = logging.LogRecord(
            "pulso.news.process", logging.ERROR, "f.py", 1, "boom", None, True
        )
        import sys

        record.exc_info = sys.exc_info()
        line = JsonLinesFormatter().format(record)

    entry = json.loads(line)
    assert entry["exception_class"] == "ValueError"
    assert PAYLOAD_SENTINEL not in line
    assert "Traceback" not in line


# --- LOG_LEVEL ---------------------------------------------------------------


def load_common(**overrides):
    """Import config/common.py fresh under a controlled environment.

    LOG_LEVEL and LOGGING live in common.py, and a fresh import of settings.py
    would reuse the already-imported config.common module.
    """

    spec = importlib.util.spec_from_file_location("config._common_log_probe", COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    # clear=True: a developer's own LOG_LEVEL can never leak into a case.
    with mock.patch.dict(os.environ, dict(overrides), clear=True):
        spec.loader.exec_module(module)
    return module


def test_log_level_defaults_to_info():
    loaded = load_common()

    assert loaded.LOG_LEVEL == "INFO"
    assert loaded.LOGGING["loggers"]["pulso"]["level"] == "INFO"


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_log_level_accepts_every_standard_level(level):
    loaded = load_common(LOG_LEVEL=level.lower())

    assert loaded.LOG_LEVEL == level
    assert loaded.LOGGING["loggers"]["pulso"]["level"] == level


@pytest.mark.parametrize("value", ["TRACE", "verbose", "", "10"])
def test_an_invalid_log_level_fails_loudly(value):
    with pytest.raises(ImproperlyConfigured, match="LOG_LEVEL must be one of"):
        load_common(LOG_LEVEL=value)


def test_the_configured_level_gates_debug_records(pulso_json_log):
    logger = logging.getLogger("pulso.news.ingest")

    logger.debug("visible at DEBUG")
    logging.getLogger("pulso").setLevel(logging.INFO)
    logger.debug("hidden at INFO")
    logger.info("visible at INFO")

    messages = [entry["message"] for entry in pulso_json_log.entries]
    assert "visible at DEBUG" in messages
    assert "visible at INFO" in messages
    assert "hidden at INFO" not in messages


# --- ingestion context ------------------------------------------------------


def test_adapter_merges_stable_context_with_call_site_fields(pulso_json_log):
    logger = ingestion_logger(run_id=5, endpoint_id=2, adapter="RSS", trigger="SCHEDULE")

    logger.info("contextual", extra={"position": 3})

    entry = pulso_json_log.entry("contextual")
    assert entry["run_id"] == 5 and entry["adapter"] == "RSS"
    # The per-call field is kept, not replaced by the adapter's context.
    assert entry["position"] == 3


def test_a_call_site_may_refine_a_context_field(pulso_json_log):
    ingestion_logger(attempt=0).warning("refined", extra={"attempt": 2})

    assert pulso_json_log.entry("refined")["attempt"] == 2


def test_bound_context_is_additive(pulso_json_log):
    ingestion_logger(run_id=1).bind(endpoint_id=9).info("bound")

    entry = pulso_json_log.entry("bound")
    assert entry["run_id"] == 1 and entry["endpoint_id"] == 9


@pytest.mark.django_db
def test_every_ingestion_record_carries_the_run_context(endpoint, monkeypatch, pulso_json_log):
    summary = ingest_fixture(endpoint, monkeypatch, "rss_valid.xml")

    entries = pulso_json_log.entries
    assert entries, "the run must emit records"
    for entry in entries:
        assert entry["run_id"] == summary.run_id
        assert entry["endpoint_id"] == endpoint.pk
        assert entry["source_id"] == endpoint.source_id
        assert entry["source_slug"] == "observed"
        assert entry["adapter"] == "RSS"
        assert entry["trigger"] == "SCHEDULE"
        assert entry["attempt"] == 0
        assert entry["task_id"] == ""


@pytest.mark.django_db
def test_run_start_record_identifies_the_execution(endpoint, monkeypatch, pulso_json_log):
    summary = ingest_fixture(endpoint, monkeypatch, "rss_valid.xml")

    start = pulso_json_log.entry("News ingestion run started")
    assert start["level"] == "INFO"
    assert start["logger"] == "pulso.news.ingest"
    assert start["run_id"] == summary.run_id
    assert start["source_slug"] == "observed" and start["adapter"] == "RSS"
    assert start["trigger"] == "SCHEDULE" and start["attempt"] == 0


@pytest.mark.django_db
def test_run_summary_record_mirrors_the_stored_result(endpoint, monkeypatch, pulso_json_log):
    summary = ingest_fixture(endpoint, monkeypatch, "rss_valid.xml")

    final = pulso_json_log.entry("News ingestion run finalized")
    assert final["status"] == "SUCCEEDED"
    assert final["error_kind"] == "" and final["http_status"] is None
    assert final["will_retry"] is False and final["duration_ms"] >= 0
    for counter in (
        "items_received",
        "items_rejected",
        "raw_created",
        "raw_changed",
        "raw_unchanged",
        "items_processed",
        "items_failed",
        "identity_duplicates",
        "content_duplicates",
        "raw_rejected",
        "source_identity_conflicts",
    ):
        assert final[counter] == getattr(summary, counter), counter


@pytest.mark.django_db
def test_failed_run_summary_reports_the_error(endpoint, monkeypatch, pulso_json_log):
    error = FetchError(FetchErrorKind.HTTP_STATUS, "Safe", http_status=404)

    class FailingAdapter:
        kind = "RSS"

        def fetch(self, request, fetcher):
            raise error

    monkeypatch.setattr("news.application.ingest.adapter_for", lambda _: FailingAdapter())
    monkeypatch.setattr(
        "news.application.ingest.Fetcher",
        type("NullFetcher", (), {"__enter__": lambda s: s, "__exit__": lambda *_: None}),
    )

    ingest_endpoint(endpoint.pk, trigger="SCHEDULE")

    final = pulso_json_log.entry("News ingestion run finalized")
    assert final["status"] == "FAILED"
    assert final["error_kind"] == "HTTP_STATUS" and final["http_status"] == 404


@pytest.mark.django_db
def test_per_item_records_carry_position_and_reason(endpoint, monkeypatch, pulso_json_log):
    ingest_fixture(endpoint, monkeypatch, "rss_id_only_entry.xml")

    intake = pulso_json_log.entry("News item rejected during intake")
    assert intake["position"] == 1 and intake["rejection_reason"] == "MISSING_IDENTITY"
    processed = pulso_json_log.entry("Raw article processed")
    assert processed["state"] == "REJECTED"
    assert processed["rejection_reason"] == "MISSING_CANONICAL_URL"
    assert processed["raw_article_id"] == RawArticle.objects.get().pk
    assert processed["article_id"] is None


@pytest.mark.django_db
def test_processing_outcome_record_reports_the_created_article(
    endpoint, monkeypatch, pulso_json_log
):
    ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v1.xml")

    processed = pulso_json_log.entry("Raw article processed")
    assert processed["state"] == "PROCESSED"
    assert processed["outcome"] == "ARTICLE_CREATED"
    assert processed["article_id"] is not None
    assert processed["rejection_reason"] == ""


@pytest.mark.django_db
def test_processing_failure_record_names_only_the_exception_class(
    endpoint, monkeypatch, pulso_json_log
):
    def explode(raw_id, *, logger=None):
        raise RuntimeError(PAYLOAD_SENTINEL)

    monkeypatch.setattr("news.application.ingest.process_raw_article", explode)

    ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v1.xml")

    failure = pulso_json_log.entry("News raw processing failed")
    assert failure["exception_class"] == "RuntimeError"
    assert failure["level"] == "ERROR"
    assert PAYLOAD_SENTINEL not in pulso_json_log.text()


@pytest.mark.django_db
def test_replayed_row_is_logged_against_the_run_processing_it(
    endpoint, monkeypatch, pulso_json_log
):
    """Receipt provenance and the current execution are different things."""

    first = ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v1.xml")
    raw = RawArticle.objects.get()
    original_run_id = raw.ingestion_run_id
    # Put the row back in the queue as an older run's unprocessed leftover.
    RawArticle.objects.filter(pk=raw.pk).update(
        status=RawArticle.Status.PENDING, outcome="", processed_at=None, article=None
    )
    pulso_json_log.stream.truncate(0)
    pulso_json_log.stream.seek(0)

    second = ingest_fixture(endpoint, monkeypatch, "rss_changed_item_v1.xml")

    processed = pulso_json_log.entry("Raw article processed")
    assert original_run_id == first.run_id and second.run_id != first.run_id
    # The record names the run that processed it now...
    assert processed["run_id"] == second.run_id
    assert processed["trigger"] == "SCHEDULE" and processed["attempt"] == 0
    # ...while the row keeps the run that received it.
    raw.refresh_from_db()
    assert raw.ingestion_run_id == original_run_id


@pytest.mark.django_db
def test_task_driven_processing_logs_endpoint_context_without_a_run(endpoint, pulso_json_log):
    payload = {
        "external_id": "solo-1",
        "url": "https://news.example/solo",
        "title": "Solo item",
        "summary_html": None,
        "content_html": "<p>Body.</p>",
        "published_at": None,
        "updated_at": None,
        "authors": [],
        "language": "en",
        "raw": {},
        "truncated": False,
    }
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key="solo-1",
        external_id="solo-1",
        url=payload["url"],
        payload=payload,
        payload_hash=payload_hash(payload),
        fetched_at=__import__("django.utils.timezone", fromlist=["timezone"]).now(),
    )

    process_raw_article(raw.pk)

    processed = pulso_json_log.entry("Raw article processed")
    assert processed["logger"] == "pulso.news.process"
    assert processed["endpoint_id"] == endpoint.pk and processed["source_slug"] == "observed"
    # No run is processing it, so no run context is invented.
    assert "run_id" not in processed and "trigger" not in processed


@pytest.mark.django_db
def test_poll_summary_reports_dispatch_counts(pulso_json_log):
    poll_due_endpoints.apply()

    entry = pulso_json_log.entry("News due-endpoint poll completed")
    assert entry["logger"] == "pulso.news.tasks"
    assert entry["dispatched"] == 0 and entry["skipped_inactive"] == 0


def test_context_field_vocabulary_is_allowlisted():
    assert set(INGESTION_CONTEXT_FIELDS) <= set(SAFE_FIELDS)


# --- diagnostics regression --------------------------------------------------


def test_diagnostics_uses_the_same_formatter_without_changing_its_message(pulso_json_log):
    result = execute_diagnostic_ping()

    entry = pulso_json_log.entry(f"diagnostic ping executed at {result['executed_at']}")
    assert entry["logger"] == "pulso.diagnostics"
    assert entry["level"] == "INFO"
    assert entry["message"].startswith("diagnostic ping executed at ")
    assert len(pulso_json_log.lines) == 1


# --- hygiene -----------------------------------------------------------------


@pytest.mark.django_db
def test_full_fixture_ingestion_leaks_no_content_at_debug(endpoint, monkeypatch, pulso_json_log):
    """The security boundary: operational metadata only, never content."""

    logging.getLogger("pulso").setLevel(logging.DEBUG)
    SourceEndpoint.objects.filter(pk=endpoint.pk).update(
        adapter_config={"credential_env": CONFIG_SENTINEL}
    )

    for name, content_type in (
        ("rss_valid.xml", "application/rss+xml"),
        ("rss_html_content.xml", "application/rss+xml"),
        ("rss_id_only_entry.xml", "application/rss+xml"),
        ("jsonfeed_valid.json", "application/feed+json"),
    ):
        if name.endswith(".json"):
            SourceEndpoint.objects.filter(pk=endpoint.pk).update(kind="JSON_FEED")
            endpoint.refresh_from_db()
        ingest_fixture(endpoint, monkeypatch, name, content_type=content_type)

    observed = pulso_json_log.text()
    assert pulso_json_log.lines, "the runs must emit records"
    for sentinel in (
        CONFIG_SENTINEL,
        HEADER_SENTINEL,
        PAYLOAD_SENTINEL,
        # Fixture article content: bodies, summaries and titles.
        "Body 1",
        "Summary 1",
        "Hello reader.",
        "Second paragraph.",
        "alert",
        "hidden comment",
        # Any HTML at all.
        "<",
        # Field names that would signal a content leak.
        "adapter_config",
        "body_text",
        "content_html",
        "summary_html",
    ):
        assert sentinel not in observed, f"{sentinel!r} leaked into a log record"

    # Every rendered line is one valid JSON object with the base fields.
    for line in pulso_json_log.lines:
        entry = json.loads(line)
        assert {"level", "logger", "message", "timestamp"} <= set(entry)


@pytest.mark.django_db
def test_record_attributes_carry_no_content(endpoint, monkeypatch, pulso_json_log):
    """Sensitive values must not be attached to a record in the first place."""

    ingest_fixture(endpoint, monkeypatch, "rss_html_content.xml")

    for record in pulso_json_log.records:
        attached = set(vars(record))
        assert attached.isdisjoint(
            {
                "payload",
                "title",
                "description",
                "body_text",
                "summary_html",
                "content_html",
                "adapter_config",
                "headers",
            }
        )
