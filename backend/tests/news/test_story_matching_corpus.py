"""The real matcher over the full #26 corpus: scenario guards and quality bounds.

Corpus Articles are embedded with `RecordedEmbeddingProvider`, which replays
the pinned local model's vectors offline (see `recorded_embeddings.py` for why
the hashing double cannot express these scenarios), then matched one by one in
publication order through `match_article`. No Story refresh runs here: this is
the matcher alone, so a Story's vector stays its first member's (the full
pipeline, with refresh, is measured by the Story Engine end-to-end suite).

The measured metrics are recorded below and asserted as bounds, so a policy
change that adds merges or splits fails here with the offending pairs named.

History. Matcher revision 1 (#28, primary rule only, matcher_key
story-match-v1;max_distance=0.18;max_time_gap_hours=48.0) measured precision
1.000, recall 0.632, false merges 0, false splits 7 (almen-river-flood,
elsby-harbor-storm-closure, varrow-council-budget-vote), unassigned 0.
Revision 2 (#36) adds the secondary event verifier and measures to the member
time range; the figures below are revision 2's.
"""

import dataclasses
from datetime import timedelta

import pytest
from django.utils import timezone
from pgvector.django import CosineDistance

from news.application import embeddings as embeddings_module
from news.application import story_matching as matching_module
from news.application import story_processing as processing_module
from news.application.embeddings import embed_articles
from news.application.story_matching import MATCH_POLICY as REAL_POLICY
from news.application.story_matching import MATCHER_KEY, match_article
from news.application.story_processing import (
    process_article,
    reconciliation_candidates,
    reprocess_article,
)
from news.application.story_refresh import refresh_candidates, refresh_story
from news.domain.story_matching import MatchReason, MatchRule, VerificationResult
from news.models import (
    Article,
    ArticleEmbedding,
    ArticleStoryProcessing,
    RawArticle,
    Story,
    StoryArticle,
    StoryEmbedding,
)
from tests.news.recorded_embeddings import (
    RecordedEmbeddingProvider,
    corpus_inputs,
    decode,
    record,
    text_key,
)
from tests.news.story_corpus import load_corpus
from tests.news.story_metrics import evaluate, primary_assignments

# Measured on corpus schema_version 1 with matcher_key
# story-match-v2;max_distance=0.18;max_time_gap_hours=48.0;
# secondary_max_distance=0.25;min_anchors=1;max_members=20 and
# embedding model fastembed:BAAI/bge-small-en-v1.5@52398278842e:
# precision 1.000, recall 1.000 (19/19), false merges 0, false splits 0,
# unassigned 0; 12 Stories for 12 events. The inputs are recorded and the order
# fixed, so the bounds carry no margin: any new merge or split is a behavior
# change to re-measure deliberately.
EXPECTED_MIN_PRECISION = 1.0
EXPECTED_MIN_RECALL = 1.0
EXPECTED_MAX_FALSE_MERGES = 0
EXPECTED_MAX_FALSE_SPLITS = 0
KNOWN_SPLIT_EVENTS: set[str] = set()
EXPECTED_STORIES = 12


def publication_order(loaded):
    records = sorted(
        loaded.corpus.articles, key=lambda record: (record.published_offset_minutes, record.id)
    )
    return [loaded.article_ids[record.id] for record in records]


def match_corpus(loaded):
    for article_id in publication_order(loaded):
        match_article(article_id, model_key=RecordedEmbeddingProvider.identity.model_key)


@pytest.fixture
def matched():
    loaded = load_corpus()
    embed_articles(list(loaded.article_ids.values()), RecordedEmbeddingProvider())
    match_corpus(loaded)
    return loaded


def story_of(loaded, fixture_id):
    return StoryArticle.objects.get(
        article_id=loaded.article_ids[fixture_id], is_primary=True
    ).story_id


def same_story(loaded, *fixture_ids):
    return len({story_of(loaded, fixture_id) for fixture_id in fixture_ids}) == 1


def separate(loaded, left_ids, right_ids):
    left = {story_of(loaded, fixture_id) for fixture_id in left_ids}
    right = {story_of(loaded, fixture_id) for fixture_id in right_ids}
    return left.isdisjoint(right)


def grouping(loaded):
    """The partition as sets of stable fixture ids, independent of Story pks."""

    names = loaded.names
    groups = {}
    for article_id, story_id in primary_assignments(loaded.article_ids.values()).items():
        groups.setdefault(story_id, set()).add(names[article_id])
    return {frozenset(group) for group in groups.values()}


def report(loaded):
    return evaluate(
        loaded.expected_events,
        primary_assignments(loaded.article_ids.values()),
        names=loaded.names,
    )


@pytest.mark.django_db
def test_corpus_quality_stays_within_the_recorded_bounds(matched):
    result = report(matched)
    diagnostics = result.describe()

    assert result.precision >= EXPECTED_MIN_PRECISION, diagnostics
    assert result.recall >= EXPECTED_MIN_RECALL, diagnostics
    assert result.false_merge_count <= EXPECTED_MAX_FALSE_MERGES, diagnostics
    assert result.false_split_count <= EXPECTED_MAX_FALSE_SPLITS, diagnostics
    assert result.unassigned_count == 0, diagnostics
    assert {split.event for split in result.false_splits} == KNOWN_SPLIT_EVENTS, diagnostics
    assert not result.false_merges, diagnostics
    assert Story.objects.count() == EXPECTED_STORIES, diagnostics


@pytest.mark.django_db
def test_every_association_is_primary_and_explained(matched):
    associations = StoryArticle.objects.all()
    assert associations.count() == len(matched.article_ids)
    assert StoryEmbedding.objects.count() == Story.objects.count()
    for association in associations:
        assert association.is_primary
        assert association.matcher_key == MATCHER_KEY
        assert association.evidence["reason"] in set(MatchReason)
        if association.method == StoryArticle.Method.MATCHED:
            assert association.similarity == pytest.approx(1 - association.evidence["distance"])
        else:
            assert association.similarity is None


@pytest.mark.django_db
def test_same_event_from_different_sources_converges(matched):
    assert same_story(matched, "harbor-storm-01", "harbor-storm-02")
    assert same_story(matched, "rail-strike-01", "rail-strike-03")
    # Revision 1's split: the second budget report sits at 0.189, just above
    # 0.18; the secondary verifier joins it on a shared name.
    assert same_story(matched, "varrow-budget-01", "varrow-budget-02")
    budget = StoryArticle.objects.get(article_id=matched.article_ids["varrow-budget-02"])
    assert budget.evidence["reason"] == MatchReason.VERIFIED_SAME_EVENT
    assert budget.evidence["rule"] == MatchRule.SECONDARY_EVENT_VERIFY
    assert 0.18 < budget.evidence["distance"] <= 0.25


@pytest.mark.django_db
def test_same_event_with_different_wording(matched):
    assert same_story(matched, "kestrel-quake-01", "kestrel-quake-02")
    # Revision 1's split: 0.199 from the (unrefreshed) harbor Story. Its
    # nearest member, harbor-storm-02, is 0.167 away and both name Elsby.
    harbor_03 = StoryArticle.objects.get(article_id=matched.article_ids["harbor-storm-03"])
    assert harbor_03.method == StoryArticle.Method.MATCHED
    assert harbor_03.story_id == story_of(matched, "harbor-storm-01")
    assert harbor_03.evidence["rule"] == MatchRule.SECONDARY_EVENT_VERIFY
    assert 0.18 < harbor_03.evidence["distance"] <= 0.25
    (check,) = harbor_03.evidence["verification"]
    assert check["member_distance"] < 0.18 and check["shared_anchors"] >= 1


@pytest.mark.django_db
def test_the_flood_reports_join_one_story_and_the_inquiry_stays_apart(matched):
    flood = ["almen-flood-01", "almen-flood-02", "almen-flood-03"]
    assert same_story(matched, *flood)
    for fixture_id in ("almen-flood-02", "almen-flood-03"):
        evidence = StoryArticle.objects.get(article_id=matched.article_ids[fixture_id]).evidence
        assert evidence["reason"] == MatchReason.VERIFIED_SAME_EVENT, fixture_id
    assert separate(matched, flood, ["almen-inquiry-01", "almen-inquiry-02"])


@pytest.mark.django_db
def test_high_lexical_overlap_between_different_events_stays_separate(matched):
    assert separate(matched, ["kestrel-quake-01", "kestrel-quake-02"], ["almen-quake-01"])
    almen = StoryArticle.objects.get(article_id=matched.article_ids["almen-quake-01"])
    # Inside the secondary band at 0.192, rejected because no name is shared.
    assert almen.evidence["reason"] == MatchReason.VERIFICATION_REJECTED
    (check,) = almen.evidence["verification"]
    assert check["story_id"] == story_of(matched, "kestrel-quake-01")
    assert check["result"] == VerificationResult.NO_SHARED_ANCHOR
    assert check["shared_anchors"] == 0


@pytest.mark.django_db
def test_same_topic_and_same_entities_stay_separate(matched):
    assert separate(
        matched,
        ["varrow-budget-01", "varrow-budget-02"],
        ["lowmere-budget-01", "lowmere-budget-02"],
    )
    assert same_story(matched, "lowmere-budget-01", "lowmere-budget-02")
    assert separate(matched, ["varrow-budget-01", "varrow-budget-02"], ["calloway-reelection-01"])
    assert same_story(matched, "calloway-reelection-01", "calloway-reelection-02")


@pytest.mark.django_db
def test_temporally_distant_similar_event_is_separated_by_time_not_distance(matched):
    harbor = story_of(matched, "harbor-storm-01")
    later = matched.article_ids["harbor-second-storm-01"]

    assert separate(
        matched,
        ["harbor-storm-01", "harbor-storm-02", "harbor-storm-04"],
        ["harbor-second-storm-01", "harbor-second-storm-02"],
    )
    assert same_story(matched, "harbor-second-storm-01", "harbor-second-storm-02")
    # Distance alone would have matched: the months-later storm is very close.
    vector = ArticleEmbedding.objects.get(article_id=later).vector
    distance = (
        StoryEmbedding.objects.filter(story_id=harbor)
        .annotate(distance=CosineDistance("vector", vector))
        .get()
        .distance
    )
    assert distance < 0.18
    assert StoryArticle.objects.get(article_id=later).evidence["candidate_count"] == 0


@pytest.mark.django_db
def test_syndicated_copy_joins_through_matching_evidence_not_duplicate_of(matched):
    copy = StoryArticle.objects.get(article_id=matched.article_ids["rail-strike-02"])
    assert (
        Article.objects.get(pk=copy.article_id).duplicate_of_id
        == matched.article_ids["rail-strike-01"]
    )
    assert same_story(matched, "rail-strike-01", "rail-strike-02", "rail-strike-03")
    assert copy.method == StoryArticle.Method.MATCHED
    assert copy.evidence["distance"] == pytest.approx(0.0, abs=1e-6)

    # Removing every duplicate_of link changes nothing: the matcher never reads it.
    before = grouping(matched)
    StoryArticle.objects.all().delete()
    Story.objects.all().delete()
    Article.objects.update(duplicate_of=None)
    match_corpus(matched)
    assert grouping(matched) == before


@pytest.mark.django_db
def test_story_drift_boundary_keeps_the_inquiry_out_of_the_flood(matched):
    flood = ["almen-flood-01", "almen-flood-02", "almen-flood-03"]
    inquiry = ["almen-inquiry-01", "almen-inquiry-02"]
    assert separate(matched, flood, inquiry)
    assert same_story(matched, *inquiry)


@pytest.mark.django_db
def test_rematching_from_scratch_reproduces_the_grouping_and_keeps_provenance():
    loaded = load_corpus()
    embed_articles(list(loaded.article_ids.values()), RecordedEmbeddingProvider())
    provenance = (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    )
    match_corpus(loaded)
    first = grouping(loaded)
    embeddings = list(ArticleEmbedding.objects.order_by("pk").values_list("pk", flat=True))

    StoryArticle.objects.all().delete()
    Story.objects.all().delete()
    assert not StoryEmbedding.objects.exists()
    match_corpus(loaded)

    assert grouping(loaded) == first
    assert list(ArticleEmbedding.objects.order_by("pk").values_list("pk", flat=True)) == embeddings
    assert (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    ) == provenance


@pytest.mark.local_embedding
def test_recording_matches_the_live_local_model():
    """Opt-in: the replayed vectors are still what the pinned model produces."""

    pytest.importorskip("fastembed")
    live = record()
    recorded = RecordedEmbeddingProvider()
    assert live["model_key"] == recorded.identity.model_key
    texts = corpus_inputs()
    for text, vector in zip(texts, recorded.embed(texts), strict=True):
        assert decode(live["vectors"][text_key(text)]) == pytest.approx(vector, abs=1e-5)


# --- convergence through the production reprocessing path (#36) ------------------------

CONVERGENCE_EVENTS = {
    "varrow-council-budget-vote",
    "almen-river-flood",
    "elsby-harbor-storm-closure",
    # Hard negatives kept in the same run.
    "almen-dam-inquiry",
    "kestrel-bay-earthquake",
    "almen-valley-earthquake",
    "lowmere-council-budget-vote",
}


def settle():
    for story_id in refresh_candidates(limit=1000):
        refresh_story(story_id)


def event_groups(loaded, article_ids):
    """Events -> set of Story ids, for the given Articles."""

    names = loaded.names
    groups = {}
    for article_id, story_id in primary_assignments(article_ids).items():
        event = loaded.corpus.article(names[article_id]).expected_event
        groups.setdefault(event, set()).add(story_id)
    return groups


@pytest.mark.django_db
def test_revision_one_splits_converge_through_reconciliation_and_survive_reprocessing(
    monkeypatch,
):
    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: RecordedEmbeddingProvider()
    )
    loaded = load_corpus()
    subset = [
        article_id
        for article_id in publication_order(loaded)
        if loaded.corpus.article(loaded.names[article_id]).expected_event in CONVERGENCE_EVENTS
    ]
    # A primary-only policy under its own key reproduces revision 1's decisions.
    primary_only = dataclasses.replace(matching_module.MATCH_POLICY, secondary_max_distance=0.1801)
    for module in (matching_module, processing_module):
        monkeypatch.setattr(module, "MATCHER_KEY", primary_only.matcher_key)
    monkeypatch.setattr(matching_module, "MATCH_POLICY", primary_only)
    for article_id in subset:
        process_article(article_id)
        settle()
    before = event_groups(loaded, subset)
    assert len(before["varrow-council-budget-vote"]) == 2
    assert len(before["almen-river-flood"]) == 3

    # Deploy the real matcher: every assignment is now stale under #29.
    for module in (matching_module, processing_module):
        monkeypatch.setattr(module, "MATCHER_KEY", MATCHER_KEY)
    monkeypatch.setattr(matching_module, "MATCH_POLICY", REAL_POLICY)
    ArticleStoryProcessing.objects.update(updated_at=timezone.now() - timedelta(hours=1))
    # Articles outside the subset were never processed and are selected too.
    stale = [article_id for article_id in reconciliation_candidates() if article_id in subset]
    assert stale == sorted(subset)
    for article_id in stale:
        process_article(article_id)
        settle()

    converged = event_groups(loaded, subset)
    assert all(len(stories) == 1 for stories in converged.values()), converged
    assert len({next(iter(stories)) for stories in converged.values()}) == len(CONVERGENCE_EVENTS)
    assert set(
        StoryArticle.objects.filter(article_id__in=subset).values_list("matcher_key", flat=True)
    ) == {MATCHER_KEY}

    # Operator reprocessing of every Article, in publication order, keeps it.
    for article_id in subset:
        reprocess_article(article_id)
        settle()
    again = event_groups(loaded, subset)
    assert all(len(stories) == 1 for stories in again.values()), again
    assert len({next(iter(stories)) for stories in again.values()}) == len(CONVERGENCE_EVENTS)
    assert StoryArticle.objects.filter(article_id__in=subset).count() == len(subset)
