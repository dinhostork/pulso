"""Integrity of the Story evaluation corpus and its loader (issue #26)."""

import json
import re
import socket
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news.models import Article, RawArticle, Source, SourceEndpoint, Story, StoryArticle
from tests.news.story_corpus import (
    CORPUS_PATH,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    REQUIRED_SCENARIOS,
    CorpusError,
    load_corpus,
    read_corpus,
)
from tests.news.story_metrics import evaluate, primary_assignments

README = CORPUS_PATH.parent / "README.md"
ANCHOR = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
ADVERSARIAL = (
    "same_event_different_wording",
    "same_topic_different_event",
    "same_entities_different_event",
    "temporally_distant_similar_event",
    "syndicated_duplicate_publication",
    "high_lexical_overlap_different_event",
    "story_drift_boundary",
    "same_conflict_different_event",
    "same_war_technology_different_event",
)


@pytest.fixture(scope="module")
def corpus():
    return read_corpus()


def words(article):
    return set(re.findall(r"[a-z0-9]+", f"{article.title} {article.body}".lower()))


def jaccard(left, right):
    return len(words(left) & words(right)) / len(words(left) | words(right))


def test_corpus_declares_one_fixed_anchor(corpus):
    assert corpus.anchor == ANCHOR
    for article in corpus.articles:
        assert corpus.published_at(article) == ANCHOR + timedelta(
            minutes=article.published_offset_minutes
        )


def test_records_have_exactly_the_documented_fields(corpus):
    raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for record in raw["articles"]:
        assert REQUIRED_FIELDS <= set(record) <= REQUIRED_FIELDS | OPTIONAL_FIELDS, record["id"]
        assert isinstance(record["expected_event"], str) and record["expected_event"]
    assert len(raw["articles"]) == len(corpus.articles)


@pytest.mark.parametrize("field", ["expected_similarity", "embedding", "threshold", "story_id"])
def test_schema_rejects_implementation_fields(tmp_path, field):
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    document["articles"][0][field] = 0.9
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CorpusError, match="unknown"):
        read_corpus(path)


def test_ids_are_stable_unique_and_catalogued(corpus):
    ids = [article.id for article in corpus.articles]
    assert len(ids) == len(set(ids))
    catalog = README.read_text(encoding="utf-8")
    for fixture_id in ids:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*-\d{2}", fixture_id), fixture_id
        assert f"`{fixture_id}`" in catalog, f"{fixture_id} missing from {README.name}"
    assert [article.id for article in read_corpus().articles] == ids


def test_canonical_urls_are_unique(corpus):
    urls = [article.canonical_url for article in corpus.articles]
    assert len(urls) == len(set(urls))


def test_every_required_scenario_exists_is_nonempty_and_catalogued(corpus):
    catalog = README.read_text(encoding="utf-8")
    for name in REQUIRED_SCENARIOS:
        assert corpus.scenarios[name].article_ids, name
        assert f"`{name}`" in catalog, name
    covered = {fixture_id for s in corpus.scenarios.values() for fixture_id in s.article_ids}
    assert covered == {article.id for article in corpus.articles}


def test_adversarial_scenarios_explain_their_labels(corpus):
    for article in corpus.articles:
        assert article.note.strip()
    for name in ADVERSARIAL:
        members = [corpus.article(fixture_id) for fixture_id in corpus.scenarios[name].article_ids]
        assert any(member.rationale for member in members), name


def test_events_have_pairs_to_measure(corpus):
    sizes = Counter(article.expected_event for article in corpus.articles)
    assert sum(size * (size - 1) // 2 for size in sizes.values()) >= 15
    assert any(size == 1 for size in sizes.values())


def scenario_events(corpus, name):
    return {
        corpus.article(fixture_id).expected_event
        for fixture_id in corpus.scenarios[name].article_ids
    }


def test_scenarios_have_the_shape_they_claim(corpus):
    a = corpus.article
    assert len(scenario_events(corpus, "same_event_different_source")) < len(
        {a(i).source_slug for i in corpus.scenarios["same_event_different_source"].article_ids}
    )
    # Same event, little shared wording.
    assert a("harbor-storm-01").expected_event == a("harbor-storm-03").expected_event
    assert jaccard(a("harbor-storm-01"), a("harbor-storm-03")) < 0.25
    # Different events, nearly the same words.
    assert a("kestrel-quake-01").expected_event != a("almen-quake-01").expected_event
    assert jaccard(a("kestrel-quake-01"), a("almen-quake-01")) > 0.6
    assert jaccard(a("kestrel-quake-01"), a("almen-quake-01")) > jaccard(
        a("kestrel-quake-01"), a("kestrel-quake-02")
    )
    # Same named entity, different events.
    assert (
        "Calloway" in a("varrow-budget-01").body and "Calloway" in a("calloway-reelection-01").body
    )
    assert len(scenario_events(corpus, "same_entities_different_event")) == 2
    assert len(scenario_events(corpus, "same_topic_different_event")) == 2
    # Time alone separates a near-repeat.
    gap = (
        a("harbor-second-storm-01").published_offset_minutes
        - a("harbor-storm-01").published_offset_minutes
    )
    assert gap > 60 * 24 * 90
    assert len(scenario_events(corpus, "temporally_distant_similar_event")) == 2
    # Later reporting joins; drift beyond the situation does not.
    assert len(scenario_events(corpus, "same_event_later_reporting")) == 2
    assert scenario_events(corpus, "story_drift_boundary") == {
        "almen-river-flood",
        "almen-dam-inquiry",
    }
    assert len(scenario_events(corpus, "clearly_unrelated_events")) >= 3
    # One war, two events, each with a second report of its own (#38).
    for name in ("same_conflict_different_event", "same_war_technology_different_event"):
        members = corpus.scenarios[name].article_ids
        assert len(scenario_events(corpus, name)) == 2, name
        assert len(members) > len(scenario_events(corpus, name)), name
        offsets = [a(fixture_id).published_offset_minutes for fixture_id in members]
        assert max(offsets) - min(offsets) < 60 * 48, name
    assert "Sarran" in a("dunmar-collapse-01").title and "Sarran" in a("sarran-strikes-01").body
    assert "drone" in a("tarvia-drone-warning-01").title
    assert "drones" in a("tarvia-delegation-drones-01").title


def test_syndicated_copy_is_a_duplicate_but_not_the_whole_event(corpus):
    copies = [article for article in corpus.articles if article.syndicated_from]
    assert copies
    for copy in copies:
        origin = corpus.article(copy.syndicated_from)
        assert (copy.title, copy.body) == (origin.title, origin.body)
        assert copy.source_slug != origin.source_slug
        assert copy.expected_event == origin.expected_event
        others = [
            article
            for article in corpus.articles
            if article.expected_event == copy.expected_event
            and article.id not in {copy.id, origin.id}
        ]
        assert others, "the event must also contain non-duplicate coverage"


@pytest.mark.django_db
def test_loader_creates_real_rows_with_anchor_derived_times(corpus):
    loaded = load_corpus(corpus)

    assert list(loaded.article_ids) == [article.id for article in corpus.articles]
    assert Article.objects.count() == len(corpus.articles)
    assert Source.objects.count() == len({article.source_slug for article in corpus.articles})
    assert not SourceEndpoint.objects.filter(is_active=True).exists()
    for record in corpus.articles:
        article = Article.objects.select_related("source", "raw_article").get(
            pk=loaded.article_ids[record.id]
        )
        expected_time = ANCHOR + timedelta(minutes=record.published_offset_minutes)
        assert article.published_at == article.first_seen_at == expected_time
        assert article.canonical_url == record.canonical_url
        assert article.source.slug == record.source_slug
        assert (article.title, article.body_text, article.language) == (
            record.title,
            record.body,
            record.language,
        )
        assert article.raw_article.article_id == article.pk


@pytest.mark.django_db
def test_loader_links_syndicated_copies_and_keeps_fingerprints_unrelated_to_events(corpus):
    loaded = load_corpus(corpus)
    ids = loaded.article_ids
    copy = Article.objects.get(pk=ids["rail-strike-02"])
    origin = Article.objects.get(pk=ids["rail-strike-01"])
    rewrite = Article.objects.get(pk=ids["rail-strike-03"])

    assert copy.duplicate_of_id == origin.pk
    assert copy.content_fingerprint == origin.content_fingerprint
    assert rewrite.content_fingerprint != origin.content_fingerprint
    assert rewrite.duplicate_of_id is None
    assert Article.objects.filter(duplicate_of__isnull=False).count() == sum(
        1 for article in corpus.articles if article.syndicated_from
    )
    fingerprints_by_event = {}
    for record in corpus.articles:
        fingerprint = Article.objects.get(pk=ids[record.id]).content_fingerprint
        fingerprints_by_event.setdefault(record.expected_event, set()).add(fingerprint)
    assert len(fingerprints_by_event["elsby-harbor-storm-closure"]) == 4


@pytest.mark.django_db
def test_loading_twice_returns_the_same_articles_in_the_same_order(corpus):
    first = load_corpus(corpus)
    counts = [model.objects.count() for model in (Source, SourceEndpoint, RawArticle, Article)]
    second = load_corpus(read_corpus())

    assert dict(second.article_ids) == dict(first.article_ids)
    assert list(second.article_ids.values()) == list(first.article_ids.values())
    assert [
        model.objects.count() for model in (Source, SourceEndpoint, RawArticle, Article)
    ] == counts
    assert list(first.article_ids.values()) == sorted(first.article_ids.values())


@pytest.mark.django_db
def test_loader_performs_no_name_resolution(corpus, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the corpus loader must not resolve any host")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    loaded = load_corpus(corpus)
    assert len(loaded.article_ids) == len(corpus.articles)


def assign(loaded, story_for_event):
    """Persist an assignment as real Story/StoryArticle rows."""

    stories = {}
    for record in loaded.corpus.articles:
        key = story_for_event(record.expected_event)
        if key not in stories:
            stories[key] = Story.objects.create(language=record.language)
        StoryArticle.objects.create(
            story=stories[key],
            article_id=loaded.article_ids[record.id],
            is_primary=True,
            method=StoryArticle.Method.MANUAL,
        )


@pytest.mark.django_db
def test_perfect_assignment_read_from_story_rows_scores_one(corpus):
    loaded = load_corpus(corpus)
    assign(loaded, lambda event: event)

    report = evaluate(
        loaded.expected_events, primary_assignments(loaded.article_ids.values()), names=loaded.names
    )
    assert (report.precision, report.recall) == (1.0, 1.0), report.describe()
    assert report.unassigned_count == 0


@pytest.mark.django_db
def test_drift_merge_read_from_story_rows_is_named_in_fixture_terms(corpus):
    loaded = load_corpus(corpus)
    drift = {"almen-river-flood", "almen-dam-inquiry"}
    assign(loaded, lambda event: "almen" if event in drift else event)
    chess = loaded.article_ids["chess-final-01"]
    StoryArticle.objects.filter(article_id=chess).delete()

    report = evaluate(
        loaded.expected_events, primary_assignments(loaded.article_ids.values()), names=loaded.names
    )

    (merge,) = report.false_merges
    assert merge.events == ("almen-dam-inquiry", "almen-river-flood")
    assert merge.pair_count == 2 * 3
    assert report.unassigned_article_ids == (chess,)
    text = report.describe()
    assert "almen-inquiry-01" in text and "almen-flood-01" in text
    assert f"UNASSIGNED {chess} (chess-final-01)" in text


def test_corpus_lives_in_the_repository():
    assert CORPUS_PATH.is_file()
    assert Path(__file__).resolve().parents[1] in CORPUS_PATH.parents
