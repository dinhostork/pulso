"""The real matcher over the full #26 corpus: scenario guards and quality bounds.

Corpus Articles are embedded with `RecordedEmbeddingProvider`, which replays
the pinned local model's vectors offline (see `recorded_embeddings.py` for why
the hashing double cannot express these scenarios), then matched one by one in
publication order through `match_article`, exactly as the pipeline would.

The measured metrics are recorded below and asserted as bounds, so a policy
change that adds merges or splits fails here with the offending pairs named.
"""

import pytest
from pgvector.django import CosineDistance

from news.application.embeddings import embed_articles
from news.application.story_matching import MATCHER_KEY, match_article
from news.domain.story_matching import MatchReason
from news.models import (
    Article,
    ArticleEmbedding,
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
# story-match-v1;max_distance=0.18;max_time_gap_hours=48.0 and embedding model
# fastembed:BAAI/bge-small-en-v1.5@52398278842e:
# precision 1.000, recall 0.632, false merges 0, false splits 7, unassigned 0.
EXPECTED_MIN_PRECISION = 1.0
EXPECTED_MIN_RECALL = 0.63
EXPECTED_MAX_FALSE_MERGES = 0
EXPECTED_MAX_FALSE_SPLITS = 7
# The documented trade-off: same-event coverage just above the threshold.
KNOWN_SPLIT_EVENTS = {
    "almen-river-flood",
    "elsby-harbor-storm-closure",
    "varrow-council-budget-vote",
}


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
    # Known split: the second budget report sits at 0.189, just above 0.18.
    assert not same_story(matched, "varrow-budget-01", "varrow-budget-02")


@pytest.mark.django_db
def test_same_event_with_different_wording(matched):
    assert same_story(matched, "kestrel-quake-01", "kestrel-quake-02")
    # Known split, in the ambiguous band: 0.199 from the harbor Story.
    harbor_03 = StoryArticle.objects.get(article_id=matched.article_ids["harbor-storm-03"])
    assert harbor_03.method == StoryArticle.Method.CREATED_STORY
    assert harbor_03.evidence["reason"] == MatchReason.ABOVE_THRESHOLD
    assert 0.18 < harbor_03.evidence["distance"] <= 0.5
    assert harbor_03.evidence["candidates"][0]["story_id"] == story_of(matched, "harbor-storm-01")


@pytest.mark.django_db
def test_high_lexical_overlap_between_different_events_stays_separate(matched):
    assert separate(matched, ["kestrel-quake-01", "kestrel-quake-02"], ["almen-quake-01"])
    almen = StoryArticle.objects.get(article_id=matched.article_ids["almen-quake-01"])
    assert almen.evidence["reason"] == MatchReason.ABOVE_THRESHOLD


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
