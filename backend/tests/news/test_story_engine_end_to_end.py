"""The complete v0.3 Story Engine over the #26 corpus, end to end (#34).

Articles are persisted by the corpus loader as News Core would leave them;
from there everything runs through `news.application`: embedding, pgvector
candidate retrieval, the deterministic decision, the association, the Story
refresh (embedding, Topics, Entities, synthesis, counters) and reprocessing,
on the real PostgreSQL/pgvector test database. See `story_pipeline.py` for the
provider and the interleaving. Tasks are used only where task behavior is the
subject (redelivery); the separate-worker path is the `celery_smoke` suite.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connections
from django.test import override_settings

from news import tasks
from news.application import story_matching as matching_module
from news.application import story_refresh as refresh_module
from news.application.story_candidates import find_candidates
from news.application.story_enrichment import compute_story_enrichment, story_text
from news.application.story_processing import process_article, reprocess_article
from news.application.story_refresh import RefreshOutcome, refresh_story
from news.domain.embeddings import story_vector
from news.domain.story_matching import MatchReason, VerificationResult
from news.models import (
    Article,
    ArticleEmbedding,
    IngestionRun,
    Story,
    StoryArticle,
    StoryEmbedding,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElementSource,
    StoryTopic,
)
from tests.news.story_corpus import load_corpus
from tests.news.story_pipeline import (
    MODEL_KEY,
    delete_derived_state,
    derived_state,
    duplicate_rows,
    grouping,
    grouping_diagnostics,
    incoherent_stories,
    live_signature,
    provenance,
    publication_order,
    row_counts,
    run_pipeline,
    settle,
    story_of,
    use_recorded_provider,
)

# Matcher v2 (#36), with each Story refreshed before the next Article is matched:
# 12 active Stories, 26 associations, exactly the corpus's 12 labeled events.
# Revision 1 produced 15 Stories here (Varrow and Almen flood stayed split);
# those historical measurements are retained in test_story_quality.py.
EXPECTED_STORIES = {
    # same_event_different_source / _different_wording / _later_reporting:
    # three Sources; harbor-storm-03 (different wording) joins at 0.168 against
    # the refreshed two-member Story vector (0.199 against harbor-storm-01 alone).
    frozenset({"harbor-storm-01", "harbor-storm-02", "harbor-storm-03", "harbor-storm-04"}),
    # temporally_distant_similar_event: five months later, its own Story.
    frozenset({"harbor-second-storm-01", "harbor-second-storm-02"}),
    # syndicated_duplicate_publication: the word-for-word copy and independent coverage.
    frozenset({"rail-strike-01", "rail-strike-02", "rail-strike-03"}),
    # same_event_different_source: the secondary verifier joins the Varrow pair.
    frozenset({"varrow-budget-01", "varrow-budget-02"}),
    # same_topic_different_event: the other council's budget vote.
    frozenset({"lowmere-budget-01", "lowmere-budget-02"}),
    # same_entities_different_event: Calloway's reelection bid.
    frozenset({"calloway-reelection-01", "calloway-reelection-02"}),
    # same_event_different_wording / high_lexical_overlap_different_event.
    frozenset({"kestrel-quake-01", "kestrel-quake-02"}),
    frozenset({"almen-quake-01"}),
    # clearly_unrelated_events.
    frozenset({"museum-maps-01", "museum-maps-02"}),
    frozenset({"chess-final-01"}),
    # same_event_later_reporting / story_drift_boundary: all three flood reports join.
    frozenset({"almen-flood-01", "almen-flood-02", "almen-flood-03"}),
    # story_drift_boundary: the inquiry is a separate event from the flood.
    frozenset({"almen-inquiry-01", "almen-inquiry-02"}),
}
EXPECTED_STORY_COUNT = 12
EXPECTED_ASSOCIATION_COUNT = 26


@pytest.fixture
def corpus(db, monkeypatch):
    use_recorded_provider(monkeypatch)
    return load_corpus()


@pytest.fixture
def pipeline(corpus):
    run_pipeline(publication_order(corpus))
    return corpus


def association(loaded, fixture_id):
    return StoryArticle.objects.get(article_id=loaded.article_ids[fixture_id], is_primary=True)


def event_articles(loaded, event):
    return [record.id for record in loaded.corpus.articles if record.expected_event == event]


def scenario_events(loaded, scenario):
    return sorted(
        {
            loaded.corpus.article(fid).expected_event
            for fid in loaded.corpus.scenarios[scenario].article_ids
        }
    )


def event_report(loaded, event):
    """One line naming the Articles, Sources and Stories of an event."""

    parts = []
    for fixture_id in event_articles(loaded, event):
        record = loaded.corpus.article(fixture_id)
        row = association(loaded, fixture_id)
        parts.append(
            f"{fixture_id} ({record.source_slug}) -> story {row.story_id} [{row.method} {row.evidence.get('reason')} d={row.evidence.get('distance')}]"
        )
    return f"{event}: " + "; ".join(parts)


def assert_one_story_per_event(loaded, events):
    failures = []
    for event in events:
        fixture_ids = event_articles(loaded, event)
        stories = {association(loaded, fixture_id).story_id for fixture_id in fixture_ids}
        sources = {loaded.corpus.article(fixture_id).source_slug for fixture_id in fixture_ids}
        if len(stories) != 1:
            failures.append(f"split across {len(stories)} Stories — " + event_report(loaded, event))
            continue
        story = Story.objects.get(pk=stories.pop())
        if story.status != Story.Status.ACTIVE:
            failures.append("event belongs to an inactive Story — " + event_report(loaded, event))
        if story.source_count != len(sources):
            failures.append(
                f"source_count {story.source_count} != {len(sources)} distinct Sources — "
                + event_report(loaded, event)
            )
    assert not failures, "\n".join(failures)


def separate(loaded, left, right):
    left_stories = {association(loaded, fixture_id).story_id for fixture_id in left}
    right_stories = {association(loaded, fixture_id).story_id for fixture_id in right}
    return left_stories.isdisjoint(right_stories)


# --- exact final state ------------------------------------------------------------------


@pytest.mark.django_db
def test_exact_story_and_association_counts_and_membership(pipeline):
    assert grouping(pipeline) == EXPECTED_STORIES, grouping_diagnostics(pipeline)
    events = {record.expected_event for record in pipeline.corpus.articles}
    assert len(events) == EXPECTED_STORY_COUNT
    assert_one_story_per_event(pipeline, sorted(events))
    assert Story.objects.filter(status=Story.Status.ACTIVE).count() == EXPECTED_STORY_COUNT
    assert StoryArticle.objects.count() == EXPECTED_ASSOCIATION_COUNT
    assert StoryArticle.objects.filter(is_primary=True).count() == EXPECTED_ASSOCIATION_COUNT
    assert StorySynthesis.objects.filter(is_current=True).count() == EXPECTED_STORY_COUNT
    assert StoryEmbedding.objects.filter(model_key=MODEL_KEY).count() == EXPECTED_STORY_COUNT
    assert StoryEmbedding.objects.count() == EXPECTED_STORY_COUNT
    assert not any(duplicate_rows().values()), duplicate_rows()
    assert incoherent_stories() == []
    for members in EXPECTED_STORIES:
        story = story_of(pipeline, next(iter(members)))
        sources = {pipeline.corpus.article(fixture_id).source_slug for fixture_id in members}
        assert (story.article_count, story.source_count) == (len(members), len(sources)), members


# --- named scenarios ---------------------------------------------------------------------


@pytest.mark.django_db
def test_article_matching_an_existing_story(pipeline):
    harbor = association(pipeline, "harbor-storm-01")
    joined = association(pipeline, "harbor-storm-02")

    assert joined.story_id == harbor.story_id
    assert joined.method == StoryArticle.Method.MATCHED
    assert joined.evidence["reason"] == MatchReason.WITHIN_THRESHOLD
    assert joined.evidence["candidates"][0]["story_id"] == harbor.story_id
    assert joined.evidence["distance"] <= joined.evidence["max_distance"] == 0.18
    assert joined.similarity == pytest.approx(1 - joined.evidence["distance"])


@pytest.mark.django_db
def test_no_qualifying_candidate_creates_a_new_story(pipeline):
    first = association(pipeline, "harbor-storm-01")
    assert first.method == StoryArticle.Method.CREATED_STORY
    assert first.evidence["reason"] == MatchReason.NO_CANDIDATES
    assert first.evidence["candidate_count"] == 0

    chess = association(pipeline, "chess-final-01")
    assert chess.method == StoryArticle.Method.CREATED_STORY
    assert chess.evidence["reason"] == MatchReason.ABOVE_THRESHOLD
    assert chess.evidence["candidate_count"] >= 1
    assert chess.evidence["distance"] > 0.18
    assert chess.similarity is None
    assert StoryArticle.objects.filter(story_id=chess.story_id).count() == 1


@pytest.mark.django_db
def test_same_event_different_source_converges_to_one_story(pipeline):
    """#34 acceptance, moved from #28: one Story per event, source_count = its Sources."""

    assert_one_story_per_event(pipeline, scenario_events(pipeline, "same_event_different_source"))


@pytest.mark.django_db
def test_varrow_budget_pair_from_different_sources_joins_one_story(corpus):
    fixture_ids = ["varrow-budget-01", "varrow-budget-02"]
    run_pipeline([corpus.article_ids[fid] for fid in fixture_ids])
    assert grouping(corpus) == {frozenset(fixture_ids)}, grouping_diagnostics(corpus)
    assert_one_story_per_event(corpus, ["varrow-council-budget-vote"])
    assert story_of(corpus, fixture_ids[0]).source_count == 2


@pytest.mark.django_db
def test_multi_source_events_group_into_one_story_each(pipeline):
    events = sorted(
        {
            record.expected_event
            for record in pipeline.corpus.articles
            if len(
                {
                    r.source_slug
                    for r in pipeline.corpus.articles
                    if r.expected_event == record.expected_event
                }
            )
            > 1
        }
    )
    assert_one_story_per_event(pipeline, events)


@pytest.mark.django_db
def test_same_event_different_wording_converges_to_one_story(pipeline):
    """#34 acceptance, moved from #28: low lexical overlap, one Story per event."""

    assert_one_story_per_event(pipeline, scenario_events(pipeline, "same_event_different_wording"))
    reworded = association(pipeline, "harbor-storm-03")
    assert reworded.method == StoryArticle.Method.MATCHED
    assert reworded.evidence["distance"] <= 0.18


@pytest.mark.django_db
def test_harbor_different_wording_pair_joins_without_an_intermediate_report(corpus):
    fixture_ids = ["harbor-storm-01", "harbor-storm-03"]
    run_pipeline([corpus.article_ids[fid] for fid in fixture_ids])
    assert grouping(corpus) == {frozenset(fixture_ids)}, grouping_diagnostics(corpus)
    assert incoherent_stories() == []


@pytest.mark.django_db
def test_same_topic_different_event_stays_separate(pipeline):
    assert separate(
        pipeline,
        ["varrow-budget-01", "varrow-budget-02"],
        ["lowmere-budget-01", "lowmere-budget-02"],
    )


@pytest.mark.django_db
def test_same_entities_different_event_stays_separate(pipeline):
    assert separate(
        pipeline,
        ["varrow-budget-01", "varrow-budget-02"],
        ["calloway-reelection-01", "calloway-reelection-02"],
    )
    assert (
        association(pipeline, "calloway-reelection-01").evidence["reason"]
        == MatchReason.ABOVE_THRESHOLD
    )


@pytest.mark.django_db
def test_high_lexical_overlap_different_event_stays_separate(pipeline):
    assert separate(pipeline, ["kestrel-quake-01", "kestrel-quake-02"], ["almen-quake-01"])
    almen = association(pipeline, "almen-quake-01")
    assert almen.evidence["reason"] == MatchReason.VERIFICATION_REJECTED
    (verification,) = almen.evidence["verification"]
    assert verification["result"] == VerificationResult.NO_SHARED_ANCHOR
    assert verification["story_id"] == association(pipeline, "kestrel-quake-01").story_id
    assert (
        almen.evidence["candidates"][0]["story_id"]
        == association(pipeline, "kestrel-quake-01").story_id
    )


@pytest.mark.django_db
def test_temporally_distant_similar_event_stays_separate(pipeline):
    assert separate(
        pipeline,
        ["harbor-storm-01", "harbor-storm-02", "harbor-storm-03", "harbor-storm-04"],
        ["harbor-second-storm-01", "harbor-second-storm-02"],
    )
    assert association(pipeline, "harbor-second-storm-01").evidence["candidate_count"] == 0


@pytest.mark.django_db
def test_almen_flood_converges_and_keeps_the_inquiry_separate(pipeline):
    assert_one_story_per_event(pipeline, ["almen-river-flood", "almen-dam-inquiry"])
    assert separate(
        pipeline,
        ["almen-flood-01", "almen-flood-02", "almen-flood-03"],
        ["almen-inquiry-01", "almen-inquiry-02"],
    )


@pytest.mark.django_db
def test_syndicated_publication_joins_through_matching_not_duplicate_of(pipeline):
    copy = association(pipeline, "rail-strike-02")
    original = pipeline.article_ids["rail-strike-01"]

    assert Article.objects.get(pk=copy.article_id).duplicate_of_id == original
    assert copy.method == StoryArticle.Method.MATCHED
    assert copy.evidence["distance"] == pytest.approx(0.0, abs=1e-6)
    story = story_of(pipeline, "rail-strike-01")
    assert {
        association(pipeline, f).story_id
        for f in ("rail-strike-01", "rail-strike-02", "rail-strike-03")
    } == {story.pk}
    # A copy is a publication, not independent reporting, but it is still a Source.
    assert (story.article_count, story.source_count) == (3, 3)


@pytest.mark.django_db
def test_later_article_causes_story_refresh(corpus):
    ids = corpus.article_ids
    run_pipeline([ids[f] for f in ("harbor-storm-01", "harbor-storm-02", "harbor-storm-03")])
    story = story_of(corpus, "harbor-storm-01")
    before_signature = story.member_signature
    before_synthesis = StorySynthesis.objects.get(story=story, is_current=True).pk
    before_vector = list(StoryEmbedding.objects.get(story=story, model_key=MODEL_KEY).vector)

    process_article(ids["harbor-storm-04"])
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE
    assert story.article_count == 3, "counters wait for the refresh"
    (result,) = settle()

    assert result.outcome == RefreshOutcome.REFRESHED and result.story_id == story.pk
    story.refresh_from_db()
    signature = live_signature(story.pk)
    assert story.member_signature == signature != before_signature
    assert (story.refresh_state, story.article_count, story.source_count) == ("CURRENT", 4, 3)
    assert story.first_published_at == Article.objects.get(pk=ids["harbor-storm-01"]).published_at
    assert story.last_published_at == Article.objects.get(pk=ids["harbor-storm-04"]).published_at
    embedding = StoryEmbedding.objects.get(story=story, model_key=MODEL_KEY)
    assert (embedding.member_count, embedding.member_signature) == (4, signature)
    assert list(embedding.vector) != before_vector
    assert_embedding_matches_members(story)
    assert_enrichment_matches_members(story)
    assert set(
        StoryTopic.objects.filter(story=story).values_list("member_signature", flat=True)
    ) == {signature}
    assert set(
        StoryEntity.objects.filter(story=story).values_list("member_signature", flat=True)
    ) == {signature}
    synthesis = StorySynthesis.objects.get(story=story, is_current=True)
    assert synthesis.pk != before_synthesis and synthesis.member_signature == signature
    assert not StorySynthesis.objects.get(pk=before_synthesis).is_current
    cited = set(
        StorySynthesisElementSource.objects.filter(element__synthesis=synthesis).values_list(
            "article_id", flat=True
        )
    )
    assert ids["harbor-storm-04"] in cited


def assert_embedding_matches_members(story):
    members = list(
        ArticleEmbedding.objects.filter(
            article__story_articles__story=story, model_key=MODEL_KEY
        ).values_list("vector", flat=True)
    )
    embedding = StoryEmbedding.objects.get(story=story, model_key=MODEL_KEY)
    assert embedding.member_count == len(members) == story.article_count
    # Use the shipped vector rule; tolerance only covers PostgreSQL float32 storage.
    assert list(embedding.vector) == pytest.approx(story_vector(members), abs=1e-6), story.pk
    assert embedding.member_signature == story.member_signature


def assert_enrichment_matches_members(story, *, topics=True, entities=True):
    recomputed = compute_story_enrichment(story_text(story.pk))
    if topics:
        assert dict(StoryTopic.objects.filter(story=story).values_list("topic__slug", "score")) == {
            slug: score for slug, (_label, score) in recomputed.topics.items()
        }, story.pk
        assert (
            not StoryTopic.objects.filter(story=story)
            .exclude(member_signature=story.member_signature)
            .exists()
        )
    if entities:
        assert {
            (kind, key): score
            for kind, key, score in StoryEntity.objects.filter(story=story).values_list(
                "entity__kind", "entity__normalized_key", "score"
            )
        } == {key: score for key, (_name, score) in recomputed.entities.items()}, story.pk
        assert (
            not StoryEntity.objects.filter(story=story)
            .exclude(member_signature=story.member_signature)
            .exists()
        )


@pytest.mark.django_db
def test_story_embedding_reflects_final_membership(pipeline):
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        assert_embedding_matches_members(story)


@pytest.mark.django_db
def test_topics_come_from_the_final_membership(pipeline):
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        assert_enrichment_matches_members(story, entities=False)


@pytest.mark.django_db
def test_entities_come_from_the_final_membership(pipeline):
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        assert_enrichment_matches_members(story, topics=False)


@pytest.mark.django_db
def test_synthesis_reflects_final_membership(pipeline):
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        synthesis = StorySynthesis.objects.get(story=story, is_current=True)
        assert synthesis.member_signature == live_signature(story.pk) == story.member_signature
        assert synthesis.elements.filter(kind="TITLE").count() == 1
    assert incoherent_stories() == []


@pytest.mark.django_db
def test_synthesis_has_traceable_article_sources_in_deterministic_order(pipeline):
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        members = set(StoryArticle.objects.filter(story=story).values_list("article_id", flat=True))
        synthesis = StorySynthesis.objects.get(story=story, is_current=True)
        assert synthesis.member_signature == story.member_signature
        elements = list(synthesis.elements.all())
        assert sum(1 for element in elements if element.kind == "TITLE") == 1
        publication_ids = list(
            Article.objects.filter(pk__in=members)
            .order_by("published_at", "pk")
            .values_list("pk", flat=True)
        )
        for element in elements:
            supporters = list(
                element.sources.order_by("position").values_list("article_id", flat=True)
            )
            assert supporters and set(supporters) <= members, (story.pk, element.kind)
            assert supporters == [pk for pk in publication_ids if pk in supporters]
            assert list(
                element.sources.order_by("position").values_list("position", flat=True)
            ) == list(range(len(supporters)))
            # Source-grounded: every element's text is carried by a supporting Article.
            texts = Article.objects.filter(pk__in=supporters).values_list("title", "body_text")
            assert any(element.text in title or element.text in body for title, body in texts)


# --- second pass, reprocessing and redelivery --------------------------------------------


@pytest.mark.django_db
def test_a_second_full_pass_creates_nothing(pipeline):
    counts = row_counts()
    ids = {
        model: sorted(model.objects.values_list("pk", flat=True))
        for model in (Story, StoryArticle, StorySynthesis, StoryEmbedding)
    }
    signatures = dict(Story.objects.values_list("pk", "member_signature"))

    run_pipeline(publication_order(pipeline))
    noop = [refresh_story(story_id) for story_id in signatures]

    assert row_counts() == counts
    assert {model: sorted(model.objects.values_list("pk", flat=True)) for model in ids} == ids
    assert dict(Story.objects.values_list("pk", "member_signature")) == signatures
    assert {result.outcome for result in noop} == {RefreshOutcome.NOOP}
    assert not any(duplicate_rows().values()), duplicate_rows()
    assert incoherent_stories() == []


def add_ingestion_run(loaded):
    """Give provenance an IngestionRun row so its bytes are compared too."""

    raw = Article.objects.get(pk=loaded.article_ids["harbor-storm-01"]).raw_article
    run = IngestionRun.objects.create(
        endpoint=raw.endpoint,
        trigger="MANUAL",
        status="SUCCEEDED",
        started_at=raw.fetched_at,
        finished_at=raw.fetched_at,
    )
    raw.__class__.objects.filter(pk=raw.pk).update(ingestion_run=run)


@pytest.mark.django_db
def test_rebuilding_all_derived_state_reproduces_the_grouping_and_keeps_provenance(corpus):
    add_ingestion_run(corpus)
    before = provenance()
    run_pipeline(publication_order(corpus))
    first = derived_state(corpus)

    delete_derived_state()
    assert not Story.objects.exists() and not StoryArticle.objects.exists()
    run_pipeline(publication_order(corpus))

    assert grouping(corpus) == EXPECTED_STORIES, grouping_diagnostics(corpus)
    assert derived_state(corpus) == first
    assert provenance() == before


@pytest.mark.django_db
def test_operator_reprocessing_of_every_article_keeps_the_grouping_and_provenance(pipeline):
    add_ingestion_run(pipeline)
    before = provenance()
    original = dict(StoryArticle.objects.values_list("article_id", "story_id"))

    for article_id in publication_order(pipeline):
        assert reprocess_article(article_id).state == "MATCHED"
        settle()

    assert grouping(pipeline) == EXPECTED_STORIES, grouping_diagnostics(pipeline)
    assert provenance() == before
    assert StoryArticle.objects.count() == EXPECTED_ASSOCIATION_COUNT
    assert Story.objects.filter(status=Story.Status.ACTIVE).count() == EXPECTED_STORY_COUNT
    # A Story emptied by reprocessing is archived with its id kept, never deleted.
    emptied = Story.objects.exclude(pk__in=StoryArticle.objects.values("story_id"))
    assert set(emptied.values_list("status", "article_count", "source_count")) <= {
        (Story.Status.ARCHIVED, 0, 0)
    }
    # Reprocessing the two singleton events leaves two empty historical rows.
    assert emptied.count() == 2
    assert Story.objects.filter(status=Story.Status.ARCHIVED).count() == 2
    assert not Story.objects.filter(
        status=Story.Status.ACTIVE, story_articles__isnull=True
    ).exists()
    empty_ids = set(emptied.values_list("pk", flat=True))
    for article_id in publication_order(pipeline):
        assert empty_ids.isdisjoint(
            candidate.story_id for candidate in find_candidates(article_id, MODEL_KEY)
        ), grouping_diagnostics(pipeline)
    assert set(original.values()) <= set(Story.objects.values_list("pk", flat=True))
    print("reprocess-all: 12 active Stories, 2 archived empty Stories, 26 associations")
    assert incoherent_stories() == []
    assert not any(duplicate_rows().values()), duplicate_rows()


def deliver(article_ids, times, monkeypatch):
    """Execute the Story task messages `times` times each, in pipeline order.

    `embed_article_story` normally queues `match_article_story`; here the queued
    message is executed directly, `times` times, like a redelivering broker.
    Refresh messages are the ones the association would have queued.
    """

    monkeypatch.setattr(tasks.match_article_story, "delay", lambda *_args: None)
    for article_id in article_ids:
        for _ in range(times):
            tasks.embed_article_story.apply(args=[article_id]).get()
        for _ in range(times):
            tasks.match_article_story.apply(args=[article_id]).get()
        stale = list(
            Story.objects.filter(refresh_state=Story.RefreshState.STALE)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for story_id in stale:
            for _ in range(times):
                tasks.refresh_story_task.apply(
                    args=[story_id], kwargs={"reason": "membership_added"}
                ).get()


@pytest.mark.django_db
def test_redelivered_task_messages_end_in_the_single_delivery_state(corpus, monkeypatch):
    add_ingestion_run(corpus)
    before = provenance()
    order = publication_order(corpus)
    deliver(order, 1, monkeypatch)
    single = derived_state(corpus)
    single_counts = row_counts()

    delete_derived_state()
    deliver(order, 2, monkeypatch)

    assert grouping(corpus) == EXPECTED_STORIES, grouping_diagnostics(corpus)
    assert derived_state(corpus) == single
    assert row_counts() == single_counts
    assert not any(duplicate_rows().values()), duplicate_rows()
    assert incoherent_stories() == []
    assert provenance() == before


# --- concurrency on real PostgreSQL connections ------------------------------------------


def in_threads(*jobs):
    def run(job):
        try:
            return job()
        finally:
            connections.close_all()

    close_old_connections()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return [future.result(timeout=60) for future in [pool.submit(run, job) for job in jobs]]


@pytest.mark.django_db(transaction=True)
def test_two_same_event_articles_processed_concurrently_create_two_stories(monkeypatch):
    """The documented v0.3 rule: with no Story yet and no lock outside PostgreSQL,
    two same-event Articles that both retrieve before either writes each create
    a Story. Later reprocessing converges them."""

    use_recorded_provider(monkeypatch)
    loaded = load_corpus()
    first, second = loaded.article_ids["harbor-storm-01"], loaded.article_ids["harbor-storm-02"]
    barrier = threading.Barrier(2, timeout=30)
    retrieve = matching_module.find_candidates

    def retrieved_together(article_id, model_key):
        candidates = retrieve(article_id, model_key)
        barrier.wait()  # both have decided on an empty candidate set
        return candidates

    monkeypatch.setattr(matching_module, "find_candidates", retrieved_together)
    results = in_threads(lambda: process_article(first), lambda: process_article(second))
    monkeypatch.setattr(matching_module, "find_candidates", retrieve)

    assert [result.state for result in results] == ["MATCHED", "MATCHED"]
    rows = {
        row.article_id: row for row in StoryArticle.objects.filter(article_id__in=[first, second])
    }
    assert Story.objects.count() == 2
    assert rows[first].story_id != rows[second].story_id
    for row in rows.values():
        assert row.method == StoryArticle.Method.CREATED_STORY
        assert row.evidence["reason"] == MatchReason.NO_CANDIDATES
    settle()

    reprocess_article(second)
    settle()

    assert grouping(loaded) == {frozenset({"harbor-storm-01", "harbor-storm-02"})}
    assert Story.objects.filter(status=Story.Status.ACTIVE).count() == 1
    assert Story.objects.get(pk=rows[second].story_id).status == Story.Status.ARCHIVED
    assert incoherent_stories() == []


@pytest.mark.django_db(transaction=True)
@override_settings(NEWS_STORY_PROCESSING_ENABLED=True)
def test_refresh_computing_while_an_association_commits_is_discarded_then_redone(monkeypatch):
    """#32's compare-and-swap: harbor-storm-01's first refresh is computing when
    harbor-storm-02's association commits. The in-flight generation is thrown
    away, the Story stays STALE, and the follow-up refresh promotes exactly the
    final two-member membership."""

    use_recorded_provider(monkeypatch)
    loaded = load_corpus()
    ids = loaded.article_ids
    dispatched = []
    monkeypatch.setattr(refresh_module, "_dispatch", lambda *args: dispatched.append(args))
    process_article(ids["harbor-storm-01"])  # created, STALE, not yet refreshed
    story = story_of(loaded, "harbor-storm-01")
    assert story.refresh_state == Story.RefreshState.STALE
    initial_embedding = list(
        StoryEmbedding.objects.filter(story=story).values_list("member_count", "member_signature")
    )

    computing, committed = threading.Event(), threading.Event()
    synthesize = refresh_module.compute_story_synthesis

    def paused(*args, **kwargs):
        computing.set()
        assert committed.wait(30)
        return synthesize(*args, **kwargs)

    monkeypatch.setattr(refresh_module, "compute_story_synthesis", paused)

    def associate():
        assert computing.wait(30)
        try:
            return process_article(ids["harbor-storm-02"])
        finally:
            committed.set()

    outcome, joined = in_threads(lambda: refresh_story(story.pk, reason="race"), associate)
    monkeypatch.setattr(refresh_module, "compute_story_synthesis", synthesize)

    assert joined.state == "MATCHED" and joined.story_id == story.pk
    assert outcome.outcome == RefreshOutcome.STALE_RETRY
    assert (story.pk, "signature_changed") in dispatched
    assert outcome.member_signature != live_signature(story.pk)
    assert outcome.article_count == 1, "the discarded snapshot had one member"
    story.refresh_from_db()
    assert story.refresh_state == Story.RefreshState.STALE
    assert (story.member_signature, story.article_count, story.refreshed_at) == ("", 0, None)
    # Nothing of the discarded one-member computation was promoted.
    assert not StorySynthesis.objects.filter(story=story).exists()
    assert not StoryTopic.objects.filter(story=story).exists()
    assert not StoryEntity.objects.filter(story=story).exists()
    assert (
        list(
            StoryEmbedding.objects.filter(story=story).values_list(
                "member_count", "member_signature"
            )
        )
        == initial_embedding
    )

    (result,) = settle()

    assert result.outcome == RefreshOutcome.REFRESHED
    story.refresh_from_db()
    assert story.member_signature == live_signature(story.pk)
    assert (story.refresh_state, story.article_count, story.source_count) == ("CURRENT", 2, 2)
    embedding = StoryEmbedding.objects.get(story=story)
    assert (embedding.member_count, embedding.member_signature) == (2, story.member_signature)
    assert incoherent_stories() == []
