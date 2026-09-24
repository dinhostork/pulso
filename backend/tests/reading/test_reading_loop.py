"""The Phase 3 reading loop over the recorded Story corpus (#52).

The #26 corpus is loaded as News Core would leave it and grouped by the real
Story services (`run_pipeline`: matcher v3, refresh, enrichment, synthesis)
with the recorded local-model vectors: no live feed, no model download and no
worker. Everything after that goes through the authenticated HTTP API on the
real PostgreSQL test database, with real JWT validation.

The corpus counts (32 Articles, 16 active Stories) are the current regression
expectation of that fixture and matcher revision, not a production property;
`test_story_engine_end_to_end.py` owns them and explains their history.

`reading-loop.json` in the mobile contract fixtures is this module's output
for one corpus Story, with database IDs renumbered and the two server-clock
timestamps pinned; the mobile integration suite decodes and renders it. Set
`PULSO_WRITE_READING_LOOP_FIXTURE=1` to rewrite it after an intended change.
"""

import copy
import json
import os
import secrets
import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from news.application.story_matching import MATCHER_KEY
from news.application.story_processing import reprocess_article
from news.application.story_refresh import mark_story_stale, withdraw_member_vector
from news.models import Article, Story, StoryArticle, StorySynthesis
from reading.models import Bookmark, FeedImpression
from tests.news.story_corpus import load_corpus
from tests.news.story_pipeline import (
    derived_state,
    grouping,
    provenance,
    publication_order,
    run_pipeline,
    settle,
    story_of,
    use_recorded_provider,
)
from tests.news.test_story_engine_end_to_end import EXPECTED_STORIES, EXPECTED_STORY_COUNT

FIXTURE = (
    Path(__file__).resolve().parents[3] / "docs" / "contracts" / "mobile-feed" / "reading-loop.json"
)
EXPECTED_ARTICLES = 32
PINNED_TIME = "2026-09-20T12:00:00Z"
HARBOR = "harbor-storm-01"
SECOND_HARBOR = "harbor-second-storm-01"
SINGLETON = "chess-final-01"


@pytest.fixture
def recorded(transactional_db, monkeypatch):
    """The corpus as ingested, its untouched provenance, then the real grouping."""

    use_recorded_provider(monkeypatch)
    loaded = load_corpus()
    before = provenance()
    run_pipeline(publication_order(loaded))
    return loaded, before


@pytest.fixture
def readers(transactional_db):
    model = get_user_model()
    return model.objects.create_user(username="loop-reader-a"), model.objects.create_user(
        username="loop-reader-b"
    )


def bearer(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


def ok(response, status=200):
    assert response.status_code == status, (response.status_code, response.content[:500])
    return response.json() if response.content else None


def feed_cards(client, *, limit=5) -> list[dict]:
    """Every Feed card, following the server cursor to the end."""

    cards, cursor = [], None
    while True:
        query = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        page = ok(client.get("/api/feed", query))
        cards.extend(page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            return cards


def source_rows(client, story_id, synthesis_id=None) -> list[dict]:
    rows, cursor = [], None
    while True:
        query = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        if synthesis_id:
            query["synthesis_id"] = synthesis_id
        page = ok(client.get(f"/api/stories/{story_id}/sources", query))
        rows.extend(page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def assert_citation_matches_article(citation: dict) -> None:
    """A serialized citation is the cited Article's own row, with its Source."""

    article = Article.objects.select_related("source").get(pk=int(citation["id"]))
    assert citation["title"] == article.title
    assert citation["canonical_url"] == article.canonical_url
    assert citation["source"] == {
        "id": str(article.source_id),
        "name": article.source.name,
        "slug": article.source.slug,
    }


def card_of(detail: dict) -> dict:
    extra = {"entities", "current_article_count", "current_source_count", "citations"}
    return {key: value for key, value in detail.items() if key not in extra | {"sources_path"}}


class Renumbering:
    """Stable fixture IDs: each kind's database IDs renumbered in ascending order."""

    BASES = {"story": 100, "synthesis": 200, "element": 300, "article": 400, "source": 500}

    def __init__(self, *documents):
        found = {kind: set() for kind in self.BASES}
        for document in documents:
            self._collect(document, found)
        self.ids = {
            kind: {real: str(base + n) for n, real in enumerate(sorted(found[kind], key=int), 1)}
            for kind, base in self.BASES.items()
        }

    def _collect(self, document, found):
        stories = [document] if "citations" in document or "elements" in document else []
        articles = document.get("results", []) + list(document.get("citations", {}).values())
        for story in stories:
            found["story"].add(story["id"])
            if story["synthesis_id"]:
                found["synthesis"].add(story["synthesis_id"])
            for element in story["elements"]:
                found["element"].add(element["id"])
                found["article"].update(element["article_ids"])
        for article in articles:
            found["article"].add(article["id"])
            found["source"].add(article["source"]["id"])

    def _article(self, row):
        row = copy.deepcopy(row)
        row["id"] = self.ids["article"][row["id"]]
        row["source"]["id"] = self.ids["source"][row["source"]["id"]]
        if row["duplicate_of_id"] is not None:
            row["duplicate_of_id"] = self.ids["article"][row["duplicate_of_id"]]
        return row

    def story(self, detail):
        story = copy.deepcopy(detail)
        story["id"] = self.ids["story"][detail["id"]]
        story["synthesis_id"] = self.ids["synthesis"][detail["synthesis_id"]]
        story["created_at"] = PINNED_TIME
        story["synthesized_at"] = PINNED_TIME
        for element in story["elements"]:
            element["id"] = self.ids["element"][element["id"]]
            element["article_ids"] = [self.ids["article"][pk] for pk in element["article_ids"]]
        if "citations" in story:
            story["citations"] = {
                self.ids["article"][pk]: self._article(row)
                for pk, row in detail["citations"].items()
            }
            story["sources_path"] = (
                f"/api/stories/{story['id']}/sources?synthesis_id={story['synthesis_id']}"
            )
        return story

    def page(self, page):
        return {**page, "results": [self._article(row) for row in page["results"]]}


def test_recorded_corpus_reaches_the_reading_api_without_changing_it(recorded, readers):
    loaded, before = recorded
    assert Article.objects.count() == EXPECTED_ARTICLES == len(loaded.corpus.articles)
    assert grouping(loaded) == EXPECTED_STORIES
    assert len(EXPECTED_STORIES) == EXPECTED_STORY_COUNT
    assert set(StoryArticle.objects.values_list("matcher_key", flat=True)) == {MATCHER_KEY}
    assert MATCHER_KEY.startswith("story-match-v3;")
    grouped = derived_state(loaded)

    reader, other = (bearer(user) for user in readers)
    cards = feed_cards(reader)
    active = Story.objects.filter(status=Story.Status.ACTIVE)
    assert sorted(int(card["id"]) for card in cards) == sorted(active.values_list("pk", flat=True))
    assert len(cards) == EXPECTED_STORY_COUNT
    assert [card["id"] for card in feed_cards(other)] == [card["id"] for card in cards]
    for card in cards:
        story = Story.objects.get(pk=int(card["id"]))
        synthesis = StorySynthesis.objects.get(story=story, is_current=True)
        members = set(StoryArticle.objects.filter(story=story).values_list("article_id", flat=True))
        assert card["content_state"] == "CURRENT"
        assert card["synthesis_id"] == str(synthesis.pk)
        assert (card["article_count"], card["source_count"]) == (
            story.article_count,
            story.source_count,
        )
        assert card["article_count"] == len(members)
        assert card["viewer"] == {"bookmarked": False}

        detail = ok(reader.get(f"/api/stories/{card['id']}"))
        assert card_of(detail) == card
        cited = {pk for element in detail["elements"] for pk in element["article_ids"]}
        assert set(detail["citations"]) == cited
        for citation in detail["citations"].values():
            assert_citation_matches_article(citation)
            assert citation["is_current_member"] is True
        sources = source_rows(reader, card["id"], detail["synthesis_id"])
        assert {int(row["id"]) for row in sources} == members
        for row in sources:
            assert_citation_matches_article(row)

    # Grouping, synthesis and Article provenance are exactly what the pipeline left.
    assert derived_state(loaded) == grouped
    assert provenance() == before


def test_a_citation_and_a_stale_generation_citation_survive_the_http_contract(recorded, readers):
    loaded, before = recorded
    reader = bearer(readers[0])
    harbor = story_of(loaded, HARBOR)
    detail = ok(reader.get(f"/api/stories/{harbor.pk}"))
    feed_card = next(card for card in feed_cards(reader) if card["id"] == str(harbor.pk))
    sources = ok(
        reader.get(
            f"/api/stories/{harbor.pk}/sources",
            {"synthesis_id": detail["synthesis_id"], "limit": 50},
        )
    )
    summary = next(element for element in detail["elements"] if element["kind"] == "SUMMARY")
    cited = detail["citations"][summary["article_ids"][0]]
    assert_citation_matches_article(cited)
    assert cited["id"] in {row["id"] for row in sources["results"]}

    # A cited Article leaves the Story. No News service moves a member of this
    # multi-source Story under the recorded corpus, so the membership change is
    # written directly, with the same vector withdrawal and invalidation the
    # matcher applies; every read below is the real API.
    moved_id = summary["article_ids"][-1]
    destination = story_of(loaded, SECOND_HARBOR)
    with transaction.atomic():
        StoryArticle.objects.filter(article_id=int(moved_id), is_primary=True).update(
            story=destination
        )
        withdraw_member_vector(harbor.pk)
        mark_story_stale(harbor.pk, reason="membership_removed")
        mark_story_stale(destination.pk, reason="membership_added")

    updating = ok(reader.get(f"/api/stories/{harbor.pk}"))
    assert updating["content_state"] == "UPDATING"
    assert updating["synthesis_id"] == detail["synthesis_id"]
    assert updating["elements"] == detail["elements"]
    moved = updating["citations"][moved_id]
    assert moved["is_current_member"] is False
    assert_citation_matches_article(moved)
    assert moved["canonical_url"] == detail["citations"][moved_id]["canonical_url"]
    current = source_rows(reader, harbor.pk, updating["synthesis_id"])
    assert moved_id not in {row["id"] for row in current}
    assert str(harbor.pk) not in {card["id"] for card in feed_cards(reader)}
    assert provenance() == before

    ids = Renumbering(detail, sources, updating)
    produced = {
        "story": ids.story(detail),
        "feed_card": ids.story(feed_card),
        "sources": ids.page(sources),
        "updating_story": ids.story(updating),
        "moved_article_id": ids.ids["article"][moved_id],
    }
    if os.environ.get("PULSO_WRITE_READING_LOOP_FIXTURE") == "1":
        FIXTURE.write_text(json.dumps(produced, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(FIXTURE.read_text()) == produced


def impression(story_id, session, position=0):
    return {
        "event_id": str(uuid.uuid4()),
        "story_id": str(story_id),
        "feed_session_id": session,
        "position": position,
        "surface": "HOME_FEED",
        "policy_version": 1,
        "occurred_at": timezone.now().isoformat().replace("+00:00", "Z"),
    }


def test_private_writes_are_idempotent_isolated_and_leave_story_facts_alone(recorded, readers):
    loaded, _ = recorded
    user_a, user_b = readers
    reader, other = bearer(user_a), bearer(user_b)
    harbor = story_of(loaded, HARBOR)
    grouped = derived_state(loaded)
    order = [card["id"] for card in feed_cards(reader)]

    # Bookmark retry after a lost response: the repeat returns the original row.
    first = ok(reader.put(f"/api/bookmarks/{harbor.pk}", {}, format="json"))
    repeat = ok(reader.put(f"/api/bookmarks/{harbor.pk}", {}, format="json"))
    assert repeat == first
    assert Bookmark.objects.filter(user=user_a).count() == 1
    ok(reader.delete(f"/api/bookmarks/{harbor.pk}"), 204)
    ok(reader.delete(f"/api/bookmarks/{harbor.pk}"), 204)
    assert not Bookmark.objects.filter(user=user_a).exists()
    ok(reader.put(f"/api/bookmarks/{harbor.pk}", {}, format="json"))

    # FeedImpression retry after a lost response: one retained row, unchanged.
    batch = {"events": [impression(harbor.pk, str(uuid.uuid4()))]}
    accepted = ok(reader.post("/api/feed-impressions", batch, format="json"))
    stored = list(FeedImpression.objects.filter(user=user_a).values())
    replayed = ok(reader.post("/api/feed-impressions", batch, format="json"))
    assert [row["outcome"] for row in accepted["results"]] == ["accepted"]
    assert [row["outcome"] for row in replayed["results"]] == ["duplicate"]
    assert list(FeedImpression.objects.filter(user=user_a).values()) == stored

    # Two accounts: B sees none of A's private state and cannot remove it.
    assert ok(other.get("/api/bookmarks")) == {"results": [], "next_cursor": None}
    assert ok(other.get(f"/api/stories/{harbor.pk}"))["viewer"] == {"bookmarked": False}
    assert all(not card["viewer"]["bookmarked"] for card in feed_cards(other))
    ok(other.delete(f"/api/bookmarks/{harbor.pk}"), 204)
    assert Bookmark.objects.filter(user=user_a, story=harbor).exists()
    assert ok(reader.get(f"/api/stories/{harbor.pk}"))["viewer"] == {"bookmarked": True}
    assert not FeedImpression.objects.filter(user=user_b).exists()

    # Bookmarks and impressions are private: shared facts and Feed order are unchanged.
    assert derived_state(loaded) == grouped
    assert [card["id"] for card in feed_cards(other)] == order
    assert [card["id"] for card in feed_cards(reader)] == order


def test_an_archived_saved_story_is_a_removable_tombstone(recorded, readers):
    loaded, _ = recorded
    user_a, _ = readers
    reader = bearer(user_a)
    original = story_of(loaded, SINGLETON)
    ok(reader.put(f"/api/bookmarks/{original.pk}", {}, format="json"))

    # Reprocess the Story's only member: it lands in a new Story and the
    # emptied one is archived by its refresh, through the real services.
    reprocess_article(loaded.article_ids[SINGLETON])
    settle()
    original.refresh_from_db()
    replacement = story_of(loaded, SINGLETON)
    assert original.status == Story.Status.ARCHIVED
    assert replacement.pk != original.pk

    saved = ok(reader.get("/api/bookmarks"))
    assert [(row["story_id"], row["availability"]) for row in saved["results"]] == [
        (str(original.pk), "UNAVAILABLE")
    ]
    assert set(saved["results"][0]) == {"story_id", "saved_at", "availability"}
    assert ok(reader.get(f"/api/stories/{original.pk}"), 410)["code"] == "story_unavailable"
    assert ok(reader.get(f"/api/stories/{replacement.pk}"))["viewer"] == {"bookmarked": False}

    ok(reader.delete(f"/api/bookmarks/{original.pk}"), 204)
    assert ok(reader.get("/api/bookmarks")) == {"results": [], "next_cursor": None}
    assert ok(reader.put(f"/api/bookmarks/{original.pk}", {}, format="json"), 410)
    assert not Bookmark.objects.filter(user=user_a).exists()


def login(username, password) -> dict:
    return ok(
        APIClient().post(
            "/api/auth/login", {"username": username, "password": password}, format="json"
        )
    )


def with_access(access) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def test_the_saved_reading_loop_over_real_login_and_session_restore(recorded):
    loaded, _ = recorded
    model = get_user_model()
    password_a, password_b = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    model.objects.create_user(username="loop-login-a", password=password_a)
    model.objects.create_user(username="loop-login-b", password=password_b)

    tokens = login("loop-login-a", password_a)
    reader = with_access(tokens["access"])
    story_id = feed_cards(reader)[0]["id"]
    ok(reader.put(f"/api/bookmarks/{story_id}", {}, format="json"))
    assert ok(reader.get(f"/api/stories/{story_id}"))["viewer"] == {"bookmarked": True}
    assert [row["story_id"] for row in ok(reader.get("/api/bookmarks"))["results"]] == [story_id]

    # Restart: only the stored refresh token survives; Saved comes from the server again.
    restored = ok(
        APIClient().post("/api/auth/refresh", {"refresh": tokens["refresh"]}, format="json")
    )
    reader = with_access(restored["access"])
    saved = ok(reader.get("/api/bookmarks"))["results"]
    assert [(row["story_id"], row["availability"]) for row in saved] == [(story_id, "AVAILABLE")]
    assert saved[0]["story"]["viewer"] == {"bookmarked": True}

    other = with_access(login("loop-login-b", password_b)["access"])
    assert ok(other.get("/api/bookmarks"))["results"] == []
    assert ok(other.get(f"/api/stories/{story_id}"))["viewer"] == {"bookmarked": False}

    ok(reader.delete(f"/api/bookmarks/{story_id}"), 204)
    assert ok(reader.get(f"/api/stories/{story_id}"))["viewer"] == {"bookmarked": False}
    assert next(card for card in feed_cards(reader) if card["id"] == story_id)["viewer"] == {
        "bookmarked": False
    }
    assert ok(reader.get("/api/bookmarks"))["results"] == []
    assert story_of(loaded, HARBOR).status == Story.Status.ACTIVE
