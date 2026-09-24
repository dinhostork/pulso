import io
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from news.models import Story
from reading.application import impressions
from reading.application.impressions import accept_feed_impressions, prune_feed_impressions
from reading.models import Bookmark, FeedImpression
from reading.views import FeedImpressionThrottle

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "mobile-feed"
NOW = datetime(2026, 9, 19, 10, 6, 30, tzinfo=UTC)
URL = "/api/feed-impressions"
SESSION = "dd42a0ee-0727-4796-bd11-dba56f1c498b"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A settable server clock; nothing in these tests sleeps."""

    state = {"now": NOW}
    monkeypatch.setattr(impressions, "_now", lambda: state["now"])
    return state


@pytest.fixture(autouse=True)
def fresh_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def users(db):
    model = get_user_model()
    return model.objects.create_user(username="exposure-a"), model.objects.create_user(
        username="exposure-b"
    )


@pytest.fixture
def pulso_output():
    logger = logging.getLogger("pulso")
    formatter = next(handler.formatter for handler in logger.handlers if handler.formatter)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


def authenticated(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def event(story_id, *, event_id=None, session=SESSION, position=0, occurred_at=None, **extra):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "story_id": str(story_id),
        "feed_session_id": session,
        "position": position,
        "surface": "HOME_FEED",
        "policy_version": 1,
        "occurred_at": occurred_at or "2026-09-19T10:06:01Z",
        **extra,
    }


def post(client, *events, **body):
    return client.post(URL, {"events": list(events), **body}, format="json")


def outcomes(response):
    return [(item["outcome"], item["code"]) for item in response.json()["results"]]


def news_writes(captured) -> list[str]:
    return [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE"}
        and '"news_' in query["sql"].split(" WHERE ", 1)[0]
    ]


@pytest.mark.django_db(transaction=True)
def test_migration_adds_impressions_to_populated_reading_state(make_story):
    executor = MigrationExecutor(connection)
    executor.migrate([("reading", "0001_initial")])
    user = get_user_model().objects.create_user(username="migrating-reader")
    story, _ = make_story(70)
    Bookmark.objects.create(user=user, story=story)
    bookmarks = list(Bookmark.objects.values_list("pk", "user_id", "story_id", "created_at"))

    executor = MigrationExecutor(connection)
    executor.migrate([("reading", "0002_feedimpression")])

    assert list(Bookmark.objects.values_list("pk", "user_id", "story_id", "created_at")) == (
        bookmarks
    )
    assert not FeedImpression.objects.exists()
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, FeedImpression._meta.db_table
        )
    story_fk = next(
        details
        for details in constraints.values()
        if details["foreign_key"] == ("news_story", "id")
    )
    assert story_fk["columns"] == ["story_id"]
    assert {
        "reading_impression_user_event_unique",
        "reading_impression_exposure_unique",
        "reading_impression_story_is_original",
        "reading_impression_position_bounded",
        "reading_impr_received_idx",
    } <= set(constraints)
    indexed = {
        tuple(details["columns"])
        for details in constraints.values()
        if details["index"] or details["unique"]
    }
    # Only the key, the retention index, the FK lookup for SET_NULL and the two
    # unique keys; the user FK relies on the unique keys' leading column.
    assert indexed == {
        ("id",),
        ("received_at",),
        ("story_id",),
        ("user_id", "event_id"),
        ("user_id", "feed_session_id", "original_story_id"),
    }


def _row(user, story_pk, **fields):
    defaults = {
        "user": user,
        "story_id": story_pk,
        "original_story_id": story_pk,
        "event_id": uuid.uuid4(),
        "feed_session_id": uuid.UUID(SESSION),
        "position": 0,
        "surface": "HOME_FEED",
        "policy_version": 1,
        "occurred_at": NOW,
        "received_at": NOW,
    }
    return FeedImpression(**{**defaults, **fields})


@pytest.mark.django_db(transaction=True)
def test_postgresql_enforces_keys_bounds_and_original_story_identity(users, make_story):
    user, other = users
    story, _ = make_story(71)
    second, _ = make_story(72)
    first = _row(user, story.pk)
    first.save()

    invalid = [
        {"event_id": first.event_id, "feed_session_id": uuid.uuid4()},
        {"event_id": uuid.uuid4()},
        {"story_id": second.pk, "original_story_id": story.pk, "feed_session_id": uuid.uuid4()},
        {"feed_session_id": uuid.uuid4(), "position": 100_001},
        {"feed_session_id": uuid.uuid4(), "position": -1},
        {"feed_session_id": uuid.uuid4(), "surface": "SAVED"},
        {"feed_session_id": uuid.uuid4(), "policy_version": 0},
        {"story_id": None, "original_story_id": 0, "feed_session_id": uuid.uuid4()},
    ]
    for fields in invalid:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _row(user, story.pk, **fields).save()

    # The same keys are independent per account and per Story/session.
    _row(other, story.pk, event_id=first.event_id).save()
    _row(user, second.pk).save()
    _row(user, story.pk, feed_session_id=uuid.uuid4(), position=100_000).save()
    assert FeedImpression.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_contract_request_is_accepted_with_server_derived_owner_and_receipt(users):
    user, other = users
    fixture = json.loads((CONTRACTS / "feed-impressions.json").read_text())
    Story.objects.create(pk=int(fixture["request"]["events"][0]["story_id"]), language="en")

    response = authenticated(user).post(URL, fixture["request"], format="json")

    assert response.status_code == 200
    assert response.json() == fixture["response"]
    assert response["Cache-Control"] == "private, no-store"
    row = FeedImpression.objects.get()
    assert (row.user_id, row.story_id, row.original_story_id) == (
        user.pk,
        9007199254740993,
        9007199254740993,
    )
    assert (row.position, row.surface, row.policy_version) == (0, "HOME_FEED", 1)
    assert row.occurred_at == datetime(2026, 9, 19, 10, 6, 1, tzinfo=UTC)
    assert row.received_at == NOW
    assert not other.feed_impressions.exists()


@pytest.mark.django_db(transaction=True)
def test_exact_replay_is_a_duplicate_that_never_mutates_the_first_row(users, make_story, clock):
    user, _ = users
    story, _ = make_story(73)
    client = authenticated(user)
    batch = [event(story.pk, position=3)]
    first = post(client, *batch)
    before = FeedImpression.objects.values().get()

    clock["now"] = NOW + timedelta(minutes=2)
    replay = post(client, *batch)

    assert outcomes(first) == [("accepted", None)]
    assert outcomes(replay) == [("duplicate", None)]
    assert FeedImpression.objects.values().get() == before


@pytest.mark.django_db(transaction=True)
def test_changed_payload_or_reused_exposure_key_conflicts_and_first_report_wins(users, make_story):
    user, _ = users
    story, _ = make_story(74)
    other_story, _ = make_story(75)
    client = authenticated(user)
    original = event(story.pk, position=4)
    post(client, original)
    before = FeedImpression.objects.values().get()

    changed = post(
        client,
        {**original, "position": 9},
        {**original, "occurred_at": "2026-09-19T10:06:02Z"},
        {**original, "feed_session_id": str(uuid.uuid4())},
        event(story.pk),
    )
    distinct = post(client, event(other_story.pk), event(story.pk, session=str(uuid.uuid4())))

    assert outcomes(changed) == [("rejected", "event_conflict")] * 4
    assert outcomes(distinct) == [("accepted", None)] * 2
    assert FeedImpression.objects.filter(event_id=original["event_id"]).values().get() == before
    assert FeedImpression.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_in_batch_repeats_behave_like_replay_in_input_order(users, make_story):
    user, _ = users
    story, _ = make_story(76)
    other_story, _ = make_story(77)
    first = event(story.pk, position=1)
    response = post(
        authenticated(user),
        first,
        first,
        {**first, "position": 2},
        event(story.pk),
        event(other_story.pk),
    )

    assert [item["event_id"] for item in response.json()["results"]][:3] == [first["event_id"]] * 3
    assert outcomes(response) == [
        ("accepted", None),
        ("duplicate", None),
        ("rejected", "event_conflict"),
        ("rejected", "event_conflict"),
        ("accepted", None),
    ]
    assert FeedImpression.objects.count() == 2
    assert FeedImpression.objects.get(event_id=first["event_id"]).position == 1


@pytest.mark.django_db(transaction=True)
def test_database_conflict_inside_a_savepoint_does_not_poison_the_batch(users, make_story):
    user, _ = users
    story, _ = make_story(78)
    later, _ = make_story(79)
    # Committed by "another request": the prefetched event IDs cannot see it,
    # so the exposure key violation is raised by PostgreSQL mid-batch.
    _row(user, story.pk).save()

    result = accept_feed_impressions(
        user=user,
        data={"events": [event(story.pk), event(later.pk)]},
        started=0.0,
    )

    assert [(item.outcome, item.code) for item in result] == [
        ("rejected", "event_conflict"),
        ("accepted", None),
    ]
    assert FeedImpression.objects.filter(story=later).count() == 1


def _parallel(user, bodies):
    barrier = threading.Barrier(len(bodies), timeout=20)

    def deliver(body):
        close_old_connections()
        try:
            barrier.wait()
            return accept_feed_impressions(user=user, data=body, started=0.0)[0]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(bodies)) as executor:
        return list(executor.map(deliver, bodies))


@pytest.mark.django_db(transaction=True)
def test_parallel_identical_delivery_persists_one_row_with_one_duplicate(users, make_story):
    user, _ = users
    story, _ = make_story(80)
    body = {"events": [event(story.pk, position=6)]}

    results = _parallel(user, [body, body])

    assert sorted(item.outcome for item in results) == ["accepted", "duplicate"]
    row = FeedImpression.objects.get()
    assert (row.position, row.received_at) == (6, NOW)


@pytest.mark.django_db(transaction=True)
def test_parallel_different_events_for_one_exposure_keep_only_the_first(users, make_story):
    user, _ = users
    story, _ = make_story(81)

    results = _parallel(
        user, [{"events": [event(story.pk, position=1)]}, {"events": [event(story.pk)]}]
    )

    assert sorted((item.outcome, item.code) for item in results) == [
        ("accepted", None),
        ("rejected", "event_conflict"),
    ]
    assert FeedImpression.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_archived_story_records_delayed_exposure_and_unknown_story_is_rejected(users, make_story):
    user, _ = users
    archived, _ = make_story(82, status=Story.Status.ARCHIVED)
    news_before = list(Story.objects.values())

    with CaptureQueriesContext(connection) as captured:
        response = post(
            authenticated(user),
            event(archived.pk),
            event(999_999_999),
        )

    assert outcomes(response) == [("accepted", None), ("rejected", "story_not_found")]
    assert FeedImpression.objects.get().story_id == archived.pk
    assert news_writes(captured) == []
    assert list(Story.objects.values()) == news_before


@pytest.mark.django_db(transaction=True)
def test_story_deletion_nulls_the_relation_and_keeps_history_deduplicated(users, make_story):
    user, _ = users
    story, _ = make_story(83)
    client = authenticated(user)
    accepted = event(story.pk, position=2)
    post(client, accepted)
    story_id = story.pk

    story.delete()

    row = FeedImpression.objects.get()
    assert (row.story_id, row.original_story_id, row.position) == (None, story_id, 2)
    # Response-loss replay after deletion still resolves to the original row,
    # while a new report for the deleted Story is unknown, not retargeted.
    assert outcomes(post(client, accepted)) == [("duplicate", None)]
    assert outcomes(post(client, event(story_id))) == [("rejected", "story_not_found")]
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _row(user, None, original_story_id=story_id).save()
    assert FeedImpression.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_account_deletion_removes_only_that_accounts_impressions(users, make_story):
    user, other = users
    story, _ = make_story(84)
    post(authenticated(user), event(story.pk))
    post(authenticated(other), event(story.pk))

    user.delete()

    assert list(FeedImpression.objects.values_list("user_id", flat=True)) == [other.pk]
    assert Story.objects.filter(pk=story.pk).exists()


VALID = "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"events": {}},
        {"events": []},
        {"events": [event(1)] * 21},
        {"events": [event(1)], "user_id": "2"},
        {"events": ["not-an-object"]},
        {"events": [event(1, user_id="2")]},
        {"events": [event(1, received_at="2026-09-19T10:06:30Z")]},
        {"events": [{key: value for key, value in event(1).items() if key != "position"}]},
        {"events": [event(1, event_id="not-a-uuid")]},
        {"events": [event(1, event_id=VALID.replace("-", ""))]},
        {"events": [event(1, session="")]},
        {"events": [{**event(1), "story_id": 1}]},
        {"events": [{**event(1), "story_id": "0"}]},
        {"events": [{**event(1), "story_id": "01"}]},
        {"events": [{**event(1), "story_id": "-1"}]},
        {"events": [{**event(1), "story_id": "9223372036854775808"}]},
        {"events": [event(1, position=-1)]},
        {"events": [event(1, position=100_001)]},
        {"events": [event(1, position=1.0)]},
        {"events": [event(1, position=True)]},
        {"events": [event(1, position="1")]},
        {"events": [{**event(1), "surface": "SAVED"}]},
        {"events": [{**event(1), "policy_version": 2}]},
        {"events": [{**event(1), "policy_version": "1"}]},
        {"events": [{**event(1), "policy_version": True}]},
        {"events": [event(1, occurred_at="2026-09-19T10:06:01")]},
        {"events": [event(1, occurred_at="yesterday")]},
        {"events": [event(1, occurred_at="2026-09-18T10:06:29Z")]},
        {"events": [event(1, occurred_at="2026-09-19T10:11:31Z")]},
        {"events": [{**event(1), "occurred_at": 1758276361}]},
    ],
)
@pytest.mark.django_db(transaction=True)
def test_structurally_invalid_batches_fail_whole_before_any_write(body, users, make_story):
    user, _ = users
    story, _ = make_story(85)

    def retarget(value):
        # Point well-formed story IDs at a real Story so only the defect fails.
        if isinstance(value, dict) and value.get("story_id") == "1":
            return {**value, "story_id": str(story.pk)}
        return value

    if isinstance(body, dict) and isinstance(body.get("events"), list) and body["events"]:
        # A valid first event proves validation is all or nothing.
        valid = event(story.pk, session=str(uuid.uuid4()))
        body = {**body, "events": [valid, *(retarget(item) for item in body["events"])][:21]}

    response = authenticated(user).post(URL, body, format="json")

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert response.json()["fields"]
    assert not FeedImpression.objects.exists()


@pytest.mark.django_db
def test_timestamp_window_boundaries_are_inclusive(users, make_story):
    user, _ = users
    story, _ = make_story(86)
    oldest = event(story.pk, occurred_at="2026-09-18T10:06:30Z")
    newest = event(story.pk, session=str(uuid.uuid4()), occurred_at="2026-09-19T10:11:30+00:00")
    offset = event(story.pk, session=str(uuid.uuid4()), occurred_at="2026-09-19T07:06:00-03:00")

    response = post(authenticated(user), oldest, newest, offset)

    assert outcomes(response) == [("accepted", None)] * 3
    assert FeedImpression.objects.get(event_id=offset["event_id"]).occurred_at == datetime(
        2026, 9, 19, 10, 6, tzinfo=UTC
    )


@pytest.mark.django_db
def test_body_media_type_and_json_limits_fail_before_parsing_or_writes(users, make_story):
    user, _ = users
    story, _ = make_story(87)
    client = authenticated(user)
    payload = json.dumps({"events": [event(story.pk)]})
    padded = payload + " " * (impressions.MAX_BODY_BYTES - len(payload))

    at_limit = client.post(URL, padded, content_type="application/json")
    FeedImpression.objects.all().delete()
    oversized = client.post(URL, padded + " ", content_type="application/json")
    wrong_type = client.post(URL, payload, content_type="text/plain")
    form = client.post(URL, {"events": "x"})
    malformed = client.post(URL, '{"events": [', content_type="application/json")
    constant = client.post(URL, '{"events": NaN}', content_type="application/json")
    binary = client.post(URL, b"\xff\xfe", content_type="application/json")

    assert at_limit.status_code == 200
    assert oversized.status_code == 413
    assert oversized.json() == {
        "code": "request_too_large",
        "detail": "The request body is too large.",
    }
    for response in (wrong_type, form, malformed, constant, binary):
        assert response.status_code == 400
        assert response.json()["code"] == "validation_error"
    assert not FeedImpression.objects.exists()


@pytest.mark.django_db
def test_endpoint_requires_authentication_and_offers_no_listing(users):
    anonymous = APIClient().post(URL, {"events": []}, format="json")
    listing = authenticated(users[0]).get(URL)

    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "not_authenticated"
    assert listing.status_code == 405
    assert not FeedImpression.objects.exists()


@pytest.mark.django_db
def test_throttle_is_scoped_per_account_and_returns_retry_after(users, make_story, monkeypatch):
    user, other = users
    story, _ = make_story(88)
    assert FeedImpressionThrottle().get_rate() == "60/min"
    monkeypatch.setattr(FeedImpressionThrottle, "rate", "2/min", raising=False)
    ticks = {"now": 1_000_000.0}
    monkeypatch.setattr(FeedImpressionThrottle, "timer", lambda self: ticks["now"])
    client = authenticated(user)

    responses = [post(client, event(story.pk, session=str(uuid.uuid4()))) for _ in range(3)]
    ticks["now"] += 20
    still_limited = post(client, event(story.pk, session=str(uuid.uuid4())))
    other_account = post(authenticated(other), event(story.pk))
    ticks["now"] += 41
    recovered = post(client, event(story.pk, session=str(uuid.uuid4())))

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[2].json() == {"code": "rate_limited", "detail": "Try again later."}
    assert responses[2]["Retry-After"] == "60"
    assert still_limited["Retry-After"] == "40"
    assert responses[2]["Cache-Control"] == "private, no-store"
    assert other_account.status_code == 200
    assert recovered.status_code == 200
    assert FeedImpression.objects.filter(user=user).count() == 3


@pytest.mark.django_db(transaction=True)
def test_logs_carry_counts_and_outcomes_but_no_reading_history(users, make_story, pulso_output):
    user, _ = users
    story, _ = make_story(89)
    accepted = event(story.pk, position=41)
    post(authenticated(user), accepted, accepted, event(999_999_999))
    authenticated(user).post(URL, {"events": "invalid"}, format="json")
    call_command("reading_prune_impressions", stdout=io.StringIO())

    lines = [json.loads(line) for line in pulso_output.getvalue().splitlines()]
    batches = [line for line in lines if line.get("operation") == "feed_impression_batch"]
    assert batches[0]["outcome"] == "processed"
    assert (
        batches[0]["accepted_count"],
        batches[0]["duplicate_count"],
        batches[0]["conflict_count"],
        batches[0]["rejected_count"],
        batches[0]["count"],
    ) == (1, 1, 0, 1, 3)
    assert "duration_ms" in batches[0]
    assert batches[1]["outcome"] == "invalid"
    text = pulso_output.getvalue()
    for secret in (accepted["event_id"], SESSION, "999999999", "HOME_FEED"):
        assert secret not in text
    history_fields = {"story_id", "event_id", "feed_session_id", "position", "occurred_at"}
    assert not any(history_fields & set(line) for line in lines)


def _received(user, story, when):
    row = _row(user, story.pk, feed_session_id=uuid.uuid4(), received_at=when)
    row.save()
    return row


@pytest.mark.django_db(transaction=True)
def test_retention_dry_run_then_bounded_repeatable_apply(users, make_story):
    user, _ = users
    story, _ = make_story(90)
    cutoff = NOW - timedelta(days=30)
    expired = [_received(user, story, cutoff - timedelta(minutes=minutes)) for minutes in (1, 2, 3)]
    kept = [_received(user, story, cutoff), _received(user, story, NOW)]

    dry = io.StringIO()
    call_command("reading_prune_impressions", stdout=dry)
    assert FeedImpression.objects.count() == 5
    assert "Dry run: 3 FeedImpression row(s)" in dry.getvalue()
    assert f"--apply --before {cutoff.isoformat()}" in dry.getvalue()

    partial = prune_feed_impressions(cutoff=cutoff, batch_size=2, max_batches=1, apply=True)
    assert (partial.eligible, partial.deleted, partial.batches) == (3, 2, 1)
    remaining = set(FeedImpression.objects.values_list("pk", flat=True))
    assert expired[0].pk in remaining and not {expired[1].pk, expired[2].pk} & remaining

    applied = io.StringIO()
    call_command(
        "reading_prune_impressions",
        "--apply",
        f"--before={cutoff.isoformat()}",
        "--batch-size=2",
        stdout=applied,
    )
    assert "Deleted 1 FeedImpression row(s)" in applied.getvalue()
    assert set(FeedImpression.objects.values_list("pk", flat=True)) == {row.pk for row in kept}

    again = io.StringIO()
    call_command("reading_prune_impressions", "--apply", stdout=again)
    assert "Deleted 0 FeedImpression row(s)" in again.getvalue()
    assert FeedImpression.objects.count() == 2
    assert Story.objects.filter(pk=story.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "arguments",
    [
        ["--days=0"],
        ["--batch-size=0"],
        [f"--batch-size={impressions.MAX_PRUNE_BATCH + 1}"],
        ["--max-batches=0"],
        ["--before=not-a-time"],
        ["--before=2026-08-20T10:06:30"],
        # Later than the 30-day cutoff would delete rows still inside retention.
        ["--before=2026-08-20T10:06:31Z"],
    ],
)
def test_retention_command_rejects_unsafe_arguments(arguments):
    with pytest.raises(CommandError):
        call_command("reading_prune_impressions", *arguments, stdout=io.StringIO())
