"""Authenticated feed, Story detail and source HTTP contracts (#47) against PostgreSQL."""

import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from celery.app.task import Task
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from news.adapters.deterministic_embeddings import DeterministicEmbeddingProvider
from news.adapters.extractive_synthesis import ExtractiveSynthesizer
from news.adapters.http import Fetcher
from news.adapters.local_embeddings import LocalEmbeddingProvider
from news.adapters.rule_based_enrichment import RuleBasedEnrichmentExtractor
from news.application.story_read import (
    EntityDTO,
    Page,
    PersistedContractError,
    SourceArticleDTO,
    SourceDTO,
    StoryCardDTO,
    StoryDetailDTO,
    StoryElementDTO,
    TopicDTO,
)
from news.models import Article, Source, Story, StoryArticle
from news.serializers import source_article
from reading.application.feed import ViewerFeedPage
from reading.models import Bookmark, FeedImpression
from reading.serializers import serialize_feed_page, serialize_story_detail
from tests.news.story_read_builders import make_article, make_published_story

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "mobile-feed"
QUERY_BUDGET = 15


def fixture(name: str):
    return json.loads((CONTRACTS / name).read_text())


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))


@pytest.fixture
def readers(db):
    model = get_user_model()
    return model.objects.create_user(username="http-reader-a"), model.objects.create_user(
        username="http-reader-b"
    )


def bearer(user) -> APIClient:
    """A client on the full request path: real JWT validation and user lookup."""

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


def without_viewer(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "viewer"}


def shape(value):
    """The contract structure of a JSON value: keys, list element shape and value kinds."""

    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return None
    return type(value).__name__


def assert_same_shape(actual, expected, path="$"):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        # Citation maps are keyed by Article ID, so compare their values instead.
        if path.endswith(".citations"):
            for key, item in actual.items():
                assert_same_shape(item, next(iter(expected.values())), f"{path}.{key}")
            return
        assert set(actual) == set(expected), path
        for key in expected:
            assert_same_shape(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), path
        if expected and actual:
            for item in actual:
                assert_same_shape(item, expected[0], f"{path}[]")
    elif expected is not None and actual is not None:
        assert type(actual) is type(expected), path


# --- Contract fixtures reproduced exactly by the serializers ---------------------------


def _time(value):
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _elements(values):
    return tuple(
        StoryElementDTO(
            item["id"], item["kind"], item["position"], item["text"], tuple(item["article_ids"])
        )
        for item in values
    )


def _source(value) -> SourceArticleDTO:
    return SourceArticleDTO(
        id=value["id"],
        title=value["title"],
        canonical_url=value["canonical_url"],
        source=SourceDTO(**value["source"]),
        published_at=_time(value["published_at"]),
        first_seen_at=_time(value["first_seen_at"]),
        byline=value["byline"],
        duplicate_of_id=value["duplicate_of_id"],
        is_current_member=value["is_current_member"],
    )


def _card_fields(value) -> dict:
    return {
        "id": value["id"],
        "language": value["language"],
        "created_at": _time(value["created_at"]),
        "first_published_at": _time(value["first_published_at"]),
        "last_published_at": _time(value["last_published_at"]),
        "content_state": value["content_state"],
        "synthesis_id": value["synthesis_id"],
        "synthesized_at": _time(value["synthesized_at"]),
        "title": value["title"],
        "elements": _elements(value["elements"]),
        "topics": tuple(TopicDTO(**topic) for topic in value["topics"]),
        "article_count": value["article_count"],
        "source_count": value["source_count"],
    }


def _detail(value) -> StoryDetailDTO:
    return StoryDetailDTO(
        **_card_fields(value),
        entities=tuple(EntityDTO(**entity) for entity in value["entities"]),
        current_article_count=value["current_article_count"],
        current_source_count=value["current_source_count"],
        citations={key: _source(item) for key, item in value["citations"].items()},
        sources_path=value["sources_path"],
    )


@pytest.mark.parametrize(
    "name",
    [
        "story-current-single-source.json",
        "story-updating.json",
        "story-preparing.json",
    ],
)
def test_detail_serializer_reproduces_contract_fixture_exactly(name):
    expected = fixture(name)
    actual = serialize_story_detail(_detail(expected), bookmarked=expected["viewer"]["bookmarked"])
    assert actual == expected


def test_viewer_fixture_differs_only_by_bookmark_decoration():
    viewers = fixture("story-current-viewers.json")
    detail = _detail(viewers["user_a"])
    assert serialize_story_detail(detail, bookmarked=False) == viewers["user_a"]
    assert serialize_story_detail(detail, bookmarked=True) == viewers["user_b"]


def test_feed_and_source_serializers_reproduce_contract_fixtures_exactly():
    feed = fixture("feed-current.json")
    cards = tuple(StoryCardDTO(**_card_fields(card)) for card in feed["results"])
    page = Page(cards, feed["next_cursor"], feed["ordering"])
    assert serialize_feed_page(ViewerFeedPage(page, frozenset())) == feed

    sources = fixture("sources-page.json")
    assert {
        "results": [source_article(_source(item)) for item in sources["results"]],
        "next_cursor": sources["next_cursor"],
    } == sources


# --- Authentication and methods --------------------------------------------------------


ENDPOINTS = ("/api/feed", "/api/stories/{id}", "/api/stories/{id}/sources")


@pytest.mark.django_db(transaction=True)
def test_every_read_requires_a_valid_access_token(readers):
    user, _ = readers
    story, _ = make_published_story(make_article())
    expired = AccessToken.for_user(user)
    expired.set_exp(lifetime=-timedelta(seconds=1))
    credentials = [
        None,
        "Bearer",
        "Bearer not-a-token",
        f"Bearer {expired}",
        f"Bearer {RefreshToken.for_user(user)}",
        f"Token {AccessToken.for_user(user)}",
    ]
    for path in ENDPOINTS:
        for header in credentials:
            client = APIClient()
            if header:
                client.credentials(HTTP_AUTHORIZATION=header)
            response = client.get(path.format(id=story.pk))
            assert response.status_code == 401, (path, header)
            assert response.json() == {
                "code": "not_authenticated",
                "detail": "Authentication credentials were not provided.",
            }
            assert response["Cache-Control"] == "private, no-store"


@pytest.mark.django_db(transaction=True)
def test_only_read_methods_are_exposed(readers):
    story, _ = make_published_story(make_article())
    client = bearer(readers[0])
    for path in ENDPOINTS:
        target = path.format(id=story.pk)
        assert client.head(target).status_code == 200
        for method in (client.post, client.put, client.patch, client.delete):
            assert method(target, {}, format="json").status_code == 405, (method, target)


# --- Contract responses ---------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_feed_matches_contract_and_decorates_only_the_viewers_bookmarks(readers):
    reader, other = readers
    first, _ = make_published_story(make_article(), title="First")
    second, _ = make_published_story(make_article(), make_article(), title="Second")
    Bookmark.objects.create(user=reader, story=first)

    mine = bearer(reader).get("/api/feed?limit=20")
    theirs = bearer(other).get("/api/feed")

    assert mine.status_code == theirs.status_code == 200
    assert mine["Cache-Control"] == "private, no-store"
    body = mine.json()
    assert_same_shape(body | {"next_cursor": "cursor"}, fixture("feed-current.json"))
    assert body["ordering"] == "story_created_desc_v1"
    assert [card["id"] for card in body["results"]] == [str(second.pk), str(first.pk)]
    assert [card["viewer"] for card in body["results"]] == [
        {"bookmarked": False},
        {"bookmarked": True},
    ]
    assert [card["viewer"] for card in theirs.json()["results"]] == [{"bookmarked": False}] * 2
    assert [without_viewer(card) for card in body["results"]] == [
        without_viewer(card) for card in theirs.json()["results"]
    ]


@pytest.mark.django_db(transaction=True)
def test_two_accounts_receive_identical_story_facts_and_sources(readers):
    reader, other = readers
    shared_source = Source.objects.create(slug="shared-http", name="Shared HTTP Source")
    story, _ = make_published_story(
        make_article(source=shared_source), make_article(source=shared_source), make_article()
    )
    Bookmark.objects.create(user=other, story=story)

    responses = [bearer(user).get(f"/api/stories/{story.pk}") for user in (reader, other)]
    sources = [bearer(user).get(f"/api/stories/{story.pk}/sources") for user in (reader, other)]

    first, second = (response.json() for response in responses)
    assert without_viewer(first) == without_viewer(second)
    assert (first["viewer"], second["viewer"]) == ({"bookmarked": False}, {"bookmarked": True})
    assert sources[0].json() == sources[1].json()
    assert (first["current_article_count"], first["current_source_count"]) == (3, 2)


@pytest.mark.django_db(transaction=True)
def test_freshness_matrix_and_status_codes(readers):
    client = bearer(readers[0])
    current, synthesis = make_published_story(make_article(), title="Current")
    stale, _ = make_published_story(make_article(), state=Story.RefreshState.STALE)
    failed, _ = make_published_story(make_article(), state=Story.RefreshState.FAILED)
    preparing = Story.objects.create(language="en")
    StoryArticle.objects.create(
        story=preparing,
        article=make_article(),
        method=StoryArticle.Method.MANUAL,
        is_primary=True,
    )
    archived, _ = make_published_story(make_article(), status=Story.Status.ARCHIVED)
    empty = Story.objects.create(language="en")

    body = client.get(f"/api/stories/{current.pk}").json()
    assert body["content_state"] == "CURRENT"
    assert body["sources_path"] == f"/api/stories/{current.pk}/sources?synthesis_id={synthesis.pk}"
    assert_same_shape(body, fixture("story-current-viewers.json")["user_a"])
    for story in (stale, failed):
        updating = client.get(f"/api/stories/{story.pk}").json()
        assert updating["content_state"] == "UPDATING"
        assert_same_shape(updating, fixture("story-updating.json"))
    prepared = client.get(f"/api/stories/{preparing.pk}")
    assert prepared.status_code == 200
    assert prepared.json()["content_state"] == "PREPARING"
    assert prepared.json()["title"] == "Story being prepared"
    assert_same_shape(prepared.json(), fixture("story-preparing.json"))
    assert client.get(f"/api/stories/{preparing.pk}/sources").status_code == 200

    errors = {item["status"]: item["body"] for item in fixture("errors.json")["examples"]}
    feed_ids = [card["id"] for card in client.get("/api/feed").json()["results"]]
    assert feed_ids == [str(current.pk)]
    for story in (archived, empty):
        for path in (f"/api/stories/{story.pk}", f"/api/stories/{story.pk}/sources"):
            response = client.get(path)
            assert (response.status_code, response.json()) == (410, errors[410])
    for path in ("/api/stories/999999999", "/api/stories/999999999/sources"):
        response = client.get(path)
        assert (response.status_code, response.json()) == (404, errors[404])


@pytest.mark.django_db(transaction=True)
def test_citations_resolve_outside_current_membership_and_the_source_page(readers):
    client = bearer(readers[0])
    moved = make_article(title="Moved publication")
    kept = [make_article(title=f"Kept {index}") for index in range(3)]
    story, _ = make_published_story(moved, *kept)
    other, _ = make_published_story(make_article())
    StoryArticle.objects.filter(story=story, article=moved).delete()
    StoryArticle.objects.create(
        story=other, article=moved, method=StoryArticle.Method.MANUAL, is_primary=False
    )
    Story.objects.filter(pk=story.pk).update(refresh_state=Story.RefreshState.STALE)

    detail = client.get(f"/api/stories/{story.pk}").json()
    page = client.get(f"/api/stories/{story.pk}/sources?limit=1").json()

    cited = {article_id for element in detail["elements"] for article_id in element["article_ids"]}
    assert cited == set(detail["citations"])
    assert detail["citations"][str(moved.pk)]["is_current_member"] is False
    assert detail["citations"][str(moved.pk)]["canonical_url"] == moved.canonical_url
    assert str(moved.pk) not in [row["id"] for row in page["results"]]
    for article in kept:
        assert detail["citations"][str(article.pk)]["is_current_member"] is True
    # Citation metadata is the Article's current row, not a stored historical snapshot.
    Article.objects.filter(pk=moved.pk).update(title="Revised after generation")
    revised = client.get(f"/api/stories/{story.pk}").json()
    assert revised["citations"][str(moved.pk)]["title"] == "Revised after generation"


@pytest.mark.django_db(transaction=True)
def test_unsafe_publication_url_is_withheld_without_failing_the_story(readers):
    client = bearer(readers[0])
    unsafe = make_article(title="Still attributable")
    story, _ = make_published_story(unsafe, make_article())
    Article.objects.filter(pk=unsafe.pk).update(canonical_url="http://127.0.0.1/admin")

    detail = client.get(f"/api/stories/{story.pk}")
    sources = client.get(f"/api/stories/{story.pk}/sources")

    assert detail.status_code == sources.status_code == 200
    citation = detail.json()["citations"][str(unsafe.pk)]
    assert (citation["title"], citation["canonical_url"]) == ("Still attributable", None)
    assert citation["source"]["name"]
    rows = {row["id"]: row for row in sources.json()["results"]}
    assert rows[str(unsafe.pk)]["canonical_url"] is None
    assert "127.0.0.1" not in detail.content.decode() + sources.content.decode()


@pytest.mark.django_db(transaction=True)
def test_source_pagination_is_keyset_and_context_bound(readers):
    client = bearer(readers[0])
    articles = [make_article() for _ in range(3)]
    story, synthesis = make_published_story(*articles)
    other_story, other_synthesis = make_published_story(make_article())
    base = f"/api/stories/{story.pk}/sources?synthesis_id={synthesis.pk}"

    first = client.get(f"{base}&limit=2").json()
    second = client.get(f"{base}&limit=2&cursor={first['next_cursor']}").json()
    assert [row["id"] for row in first["results"] + second["results"]] == [
        str(article.pk) for article in sorted(articles, key=lambda row: row.pk)
    ]
    assert_same_shape(first | {"next_cursor": None}, fixture("sources-page.json"))
    assert second["next_cursor"] is None

    again = client.get(f"{base}&limit=2").json()
    articles[0].title = "Revised publication"
    articles[0].save(update_fields=["title", "updated_at"])
    changed = client.get(f"{base}&limit=2&cursor={again['next_cursor']}")
    assert changed.status_code == 409
    assert changed.json() == {
        "code": "source_context_changed",
        "detail": "The source list changed; restart pagination.",
    }
    # A cursor for one context never continues another.
    other = client.get(f"/api/stories/{story.pk}/sources?limit=2&cursor={again['next_cursor']}")
    assert other.status_code == 409
    foreign = client.get(f"/api/stories/{story.pk}/sources?synthesis_id={other_synthesis.pk}")
    assert foreign.status_code == 400
    assert foreign.json()["fields"] == {"synthesis_id": ["Does not belong to this Story."]}
    garbage = client.get(f"/api/stories/{other_story.pk}/sources?limit=1&cursor=x")
    assert garbage.json()["code"] == "invalid_cursor"


@pytest.mark.django_db(transaction=True)
def test_invalid_ids_limits_cursors_and_fields_are_bounded_errors(readers):
    client = bearer(readers[0])
    story, _ = make_published_story(make_article())
    for bad_id in ("abc", "0", "007", "-1", "1.5", "99999999999999999999"):
        for path in (f"/api/stories/{bad_id}", f"/api/stories/{bad_id}/sources"):
            response = client.get(path)
            assert response.status_code == 400, path
            assert response.json() == {
                "code": "validation_error",
                "detail": "The request is invalid.",
                "fields": {"story_id": ["Must be a positive decimal ID."]},
            }
    limit_error = fixture("errors.json")["examples"][1]["body"]
    for limit in ("0", "51", "x", "1.5", "-1"):
        for path in ("/api/feed", f"/api/stories/{story.pk}/sources"):
            response = client.get(f"{path}?limit={limit}")
            assert (response.status_code, response.json()) == (400, limit_error), (path, limit)
    for path in ("/api/feed", f"/api/stories/{story.pk}/sources"):
        tampered = client.get(f"{path}?cursor=v1.not-signed")
        assert (tampered.status_code, tampered.json()["code"]) == (400, "invalid_cursor")
        oversized = client.get(f"{path}?cursor={'a' * 4097}")
        assert oversized.status_code == 400
    for path in (
        "/api/feed?user_id=2",
        "/api/feed?ordering=title",
        f"/api/stories/{story.pk}?viewer=2",
        f"/api/stories/{story.pk}/sources?offset=10",
        f"/api/stories/{story.pk}/sources?synthesis_id=abc",
    ):
        response = client.get(path)
        assert response.status_code == 400, path
        assert response.json()["code"] == "validation_error"
    # A feed cursor is scoped to the feed; it cannot page sources.
    many = [make_published_story(make_article())[0] for _ in range(2)]
    feed_cursor = client.get("/api/feed?limit=1").json()["next_cursor"]
    assert many and feed_cursor
    response = client.get(f"/api/stories/{story.pk}/sources?limit=1&cursor={feed_cursor}")
    assert response.json()["code"] == "invalid_cursor"


# --- Bounded work and safety ----------------------------------------------------------


def _count(client, path) -> int:
    with CaptureQueriesContext(connection) as queries:
        assert client.get(path).status_code == 200
    return len(queries)


@pytest.mark.django_db(transaction=True)
def test_full_request_query_budget_has_no_per_card_growth(readers):
    reader, _ = readers
    client = bearer(reader)
    first, _ = make_published_story(make_article())
    Bookmark.objects.create(user=reader, story=first)
    one = _count(client, "/api/feed?limit=1")
    for index in range(49):
        story, _ = make_published_story(make_article(), make_article())
        if index % 2:
            Bookmark.objects.create(user=reader, story=story)
    fifty = _count(client, "/api/feed?limit=50")
    detail = _count(client, f"/api/stories/{first.pk}")
    sources = _count(client, f"/api/stories/{first.pk}/sources?limit=50")

    # Measured: feed 10 (JWT user + 8 snapshot statements + bookmarks), detail 13, sources 6.
    assert one == fifty <= QUERY_BUDGET
    assert detail <= QUERY_BUDGET
    assert sources <= QUERY_BUDGET
    assert len(bearer(reader).get("/api/feed?limit=50").json()["results"]) == 50


@pytest.mark.django_db(transaction=True)
def test_reads_never_write_process_dispatch_or_fetch(readers, monkeypatch):
    reader, _ = readers
    story, _ = make_published_story(make_article(), make_article())
    Bookmark.objects.create(user=reader, story=story)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a product read invoked processing, dispatch or fetching")

    for target, name in (
        (Task, "apply_async"),
        (Fetcher, "__init__"),
        (DeterministicEmbeddingProvider, "embed"),
        (LocalEmbeddingProvider, "embed"),
        (ExtractiveSynthesizer, "synthesize"),
        (RuleBasedEnrichmentExtractor, "extract_topics"),
        (RuleBasedEnrichmentExtractor, "extract_entities"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    for dotted in (
        "news.application.story_refresh.refresh_story",
        "news.application.story_refresh.schedule_refresh",
        "news.application.story_processing.embed_step",
        "news.application.story_processing.match_step",
    ):
        monkeypatch.setattr(dotted, forbidden)
    client = bearer(reader)

    with CaptureQueriesContext(connection) as queries:
        for path in ENDPOINTS:
            target = path.format(id=story.pk)
            assert client.get(target).status_code == 200
            assert client.head(target).status_code == 200

    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE"}
    ]
    assert writes == []
    assert not FeedImpression.objects.exists()


FORBIDDEN_KEYS = {
    "body_text",
    "body",
    "payload",
    "payload_hash",
    "raw_article_id",
    "member_signature",
    "signature",
    "embedding",
    "vector",
    "model_key",
    "matcher_key",
    "refresh_state",
    "status",
    "content_fingerprint",
    "error_kind",
    "external_id",
    "evidence",
}


def _keys(value) -> set[str]:
    """Every field name in a response; citation maps are keyed by Article ID, not names."""

    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if not key.isdigit():
                keys.add(key)
            keys |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _keys(item)
    return keys


@pytest.mark.django_db(transaction=True)
def test_responses_expose_no_article_body_or_operational_fields(readers):
    client = bearer(readers[0])
    article = make_article()
    story, synthesis = make_published_story(article, make_article())
    story.refresh_from_db()

    bodies = [client.get(path.format(id=story.pk)) for path in ENDPOINTS]

    for response in bodies:
        assert not _keys(response.json()) & FORBIDDEN_KEYS
        text = response.content.decode()
        for secret in (
            article.body_text,
            story.member_signature,
            synthesis.model_key,
            article.content_fingerprint,
        ):
            assert secret not in text


@pytest.mark.django_db(transaction=True)
def test_read_logs_carry_bounded_outcomes_only(readers):
    client = bearer(readers[0])
    story, _ = make_published_story(make_article(title="Loggable title"))
    logger = logging.getLogger("pulso")
    formatter = next(handler.formatter for handler in logger.handlers if handler.formatter)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        client.get("/api/feed")
        client.get(f"/api/stories/{story.pk}")
        client.get(f"/api/stories/{story.pk}/sources")
        client.get("/api/stories/999999999")
        client.get("/api/feed?cursor=v1.not-signed")
    finally:
        logger.removeHandler(handler)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    reads = [line for line in lines if line.get("message") == "Story read request completed"]
    assert [(line["operation"], line["outcome"]) for line in reads] == [
        ("feed_read", "success"),
        ("story_detail_read", "success"),
        ("story_sources_read", "success"),
        ("story_detail_read", "story_not_found"),
        ("feed_read", "invalid_cursor"),
    ]
    assert all("duration_ms" in line for line in reads)
    text = stream.getvalue()
    for secret in ("Loggable title", "example.org", "not-signed", "http-reader"):
        assert secret not in text


@pytest.mark.django_db(transaction=True)
def test_corrupt_persisted_generation_returns_the_generic_server_error(readers, monkeypatch):
    story, _ = make_published_story(make_article())

    def corrupt(*_args, **_kwargs):
        raise PersistedContractError("missing cited publication")

    monkeypatch.setattr("reading.application.feed.story_detail", corrupt)
    response = bearer(readers[0]).get(f"/api/stories/{story.pk}")

    errors = {item["status"]: item["body"] for item in fixture("errors.json")["examples"]}
    assert (response.status_code, response.json()) == (500, errors[500])
    assert "missing cited" not in response.content.decode()
    assert response["Cache-Control"] == "private, no-store"
