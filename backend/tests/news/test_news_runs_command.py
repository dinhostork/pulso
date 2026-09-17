"""`manage.py news_runs`: run history and stale-run detection (issue #20)."""

import pathlib
from datetime import timedelta
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from news.models import IngestionRun, Source, SourceEndpoint


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def endpoint(db):
    source = Source.objects.create(slug="publisher", name="Publisher")
    return SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url="https://feed.example/rss"
    )


def run(*args, **options):
    out = StringIO()
    call_command("news_runs", *args, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def listed_ids(output: str) -> list[str]:
    """The run ids in the first column, in printed order."""

    return [line.split()[0] for line in output.splitlines()[1:] if line.strip()]


def make_run(endpoint, *, age_seconds=0, status=IngestionRun.Status.SUCCEEDED, **changes):
    started = timezone.now() - timedelta(seconds=age_seconds)
    fields = {
        "endpoint": endpoint,
        "trigger": "SCHEDULE",
        "status": status,
        "started_at": started,
        "finished_at": None if status == IngestionRun.Status.RUNNING else started,
    }
    fields.update(changes)
    return IngestionRun.objects.create(**fields)


@pytest.mark.django_db
def test_last_limits_and_orders_newest_first(endpoint):
    runs = [make_run(endpoint, age_seconds=600 - index * 60) for index in range(8)]

    output = run("--last", "5")

    printed = listed_ids(output)
    # Newest first, exactly five of the eight.
    assert printed == [str(run_row.pk) for run_row in reversed(runs[-5:])]
    assert len(printed) == 5


@pytest.mark.django_db
def test_runs_started_together_use_a_deterministic_tie_breaker(endpoint):
    moment = timezone.now()
    first = make_run(endpoint, started_at=moment)
    second = make_run(endpoint, started_at=moment)

    output = run("--last", "2")

    assert listed_ids(output) == [str(second.pk), str(first.pk)]


@pytest.mark.django_db
def test_table_reports_status_trigger_counters_and_error(endpoint):
    make_run(
        endpoint,
        status=IngestionRun.Status.PARTIAL,
        trigger="MANUAL",
        attempt=2,
        items_received=10,
        items_rejected=1,
        raw_created=9,
        identity_duplicates=3,
        content_duplicates=1,
        raw_rejected=2,
        source_identity_conflicts=1,
        items_failed=1,
        error_kind="TIMEOUT",
        duration_ms=1234,
    )

    output = run()

    header, row = output.splitlines()[0], output.splitlines()[1]
    for column in ("ID", "ENDPOINT", "STATUS", "TRIGGER", "IDDUP", "SRCCONF", "ERROR", "MS"):
        assert column in header
    for value in ("PARTIAL", "MANUAL", "TIMEOUT", "1234", "10"):
        assert value in row


@pytest.mark.django_db
def test_endpoint_filter_accepts_an_id_or_an_exact_url(endpoint):
    other = SourceEndpoint.objects.create(
        source=endpoint.source, kind="RSS", url="https://other.example/rss"
    )
    mine = make_run(endpoint)
    theirs = make_run(other)

    by_id = run("--endpoint", str(endpoint.pk))
    by_url = run("--endpoint", other.url)

    assert listed_ids(by_id) == [str(mine.pk)]
    assert listed_ids(by_url) == [str(theirs.pk)]


@pytest.mark.django_db
def test_empty_history_is_stated_explicitly(endpoint):
    assert "No ingestion runs." in run()


@pytest.mark.django_db
def test_stale_lists_an_old_running_run_and_exits_nonzero(endpoint):
    stale = make_run(
        endpoint,
        age_seconds=settings.NEWS_STALE_RUNNING_SECONDS + 20,
        status=IngestionRun.Status.RUNNING,
    )

    with pytest.raises(CommandError) as error:
        run("--stale")

    assert "1 stale RUNNING run(s)" in str(error.value)
    assert error.value.returncode == 1
    # The table is printed before the nonzero exit.
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("news_runs", "--stale", stdout=out, stderr=StringIO())
    assert listed_ids(out.getvalue()) == [str(stale.pk)]


@pytest.mark.django_db
def test_a_fresh_running_run_is_not_stale(endpoint):
    make_run(
        endpoint,
        age_seconds=settings.NEWS_STALE_RUNNING_SECONDS - 20,
        status=IngestionRun.Status.RUNNING,
    )

    assert "No stale runs." in run("--stale")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        IngestionRun.Status.SUCCEEDED,
        IngestionRun.Status.PARTIAL,
        IngestionRun.Status.NO_CHANGE,
        IngestionRun.Status.FAILED,
    ],
)
def test_an_old_finalized_run_is_never_stale(endpoint, status):
    make_run(endpoint, age_seconds=30 * 86400, status=status)

    assert "No stale runs." in run("--stale")


@pytest.mark.django_db
def test_stale_exits_zero_when_nothing_is_stale(endpoint):
    make_run(endpoint, age_seconds=10, status=IngestionRun.Status.RUNNING)
    make_run(endpoint, age_seconds=99999)

    # No CommandError means exit code 0 for the shell.
    assert "No stale runs." in run("--stale")


@pytest.mark.django_db
def test_stale_can_be_limited_to_one_endpoint(endpoint):
    other = SourceEndpoint.objects.create(
        source=endpoint.source, kind="RSS", url="https://other.example/rss"
    )
    make_run(
        other,
        age_seconds=settings.NEWS_STALE_RUNNING_SECONDS + 20,
        status=IngestionRun.Status.RUNNING,
    )

    assert "No stale runs." in run("--stale", "--endpoint", str(endpoint.pk))
    with pytest.raises(CommandError):
        run("--stale", "--endpoint", str(other.pk))


def test_the_stale_threshold_is_one_shared_operational_constant():
    """The poller's in-flight guard and `--stale` must never drift apart."""

    from news import tasks
    from news.application.operations import stale_running_cutoff

    now = timezone.now()
    cutoff = stale_running_cutoff(now)

    assert (now - cutoff).total_seconds() == settings.NEWS_STALE_RUNNING_SECONDS
    assert settings.NEWS_STALE_RUNNING_SECONDS == 180
    # No second definition lives in the task module any more.
    assert not hasattr(tasks, "RUNNING_RUN_GRACE_SECONDS")
    assert "NEWS_STALE_RUNNING_SECONDS" in pathlib.Path(tasks.__file__).read_text(encoding="utf-8")


@pytest.mark.django_db
@pytest.mark.parametrize("identifier", ["999999", "https://unknown.example/rss"])
def test_an_unknown_endpoint_is_an_operator_error(endpoint, identifier):
    with pytest.raises(CommandError, match="No SourceEndpoint"):
        run("--endpoint", identifier)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0", "-3"])
def test_last_must_be_positive(endpoint, value):
    with pytest.raises(CommandError, match="--last must be a positive integer"):
        run("--last", value)
