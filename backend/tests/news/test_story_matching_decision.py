"""Pure Story matching decision: table-driven rules, policy identity, threshold evidence."""

import ast
import dataclasses
import inspect
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.conf import settings

from news.domain import story_matching
from news.domain.stories import STORY_ACTIVE, STORY_ARCHIVED, StoryCandidate
from news.domain.story_matching import (
    POLICY_REVISION,
    ArticleMatchSnapshot,
    MatchDecision,
    MatchKind,
    MatchPolicy,
    MatchReason,
    decide_story_match,
)

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
POLICY = MatchPolicy(max_distance=0.18, max_time_gap_hours=48)
ARTICLE = ArticleMatchSnapshot(article_id=1, language="en", event_time=NOW)


def candidate(story_id, distance, *, hours_ago=1, language="en", status=STORY_ACTIVE):
    return StoryCandidate(
        story_id=story_id,
        distance=distance,
        member_count=1,
        last_article_published_at=NOW - timedelta(hours=hours_ago),
        language=language,
        status=status,
    )


MATCH = MatchKind.MATCH
CREATE = MatchKind.CREATE_NEW_STORY
CASES = {
    "no candidates": ([], CREATE, MatchReason.NO_CANDIDATES, None, None),
    "nearest within threshold": (
        [candidate(7, 0.30), candidate(3, 0.05), candidate(9, 0.12)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        3,
        0.05,
    ),
    "threshold is inclusive": ([candidate(4, 0.18)], MATCH, MatchReason.WITHIN_THRESHOLD, 4, 0.18),
    "ambiguous band creates a Story": (
        [candidate(4, 0.181), candidate(5, 0.45)],
        CREATE,
        MatchReason.ABOVE_THRESHOLD,
        None,
        0.181,
    ),
    "equal distance ties on the lower story id": (
        [candidate(12, 0.1), candidate(8, 0.1), candidate(10, 0.1)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        8,
        0.1,
    ),
    "archived nearest is treated as absent": (
        [candidate(2, 0.01, status=STORY_ARCHIVED), candidate(6, 0.1)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        6,
        0.1,
    ),
    "only archived candidates": (
        [candidate(2, 0.01, status=STORY_ARCHIVED)],
        CREATE,
        MatchReason.NO_COMPATIBLE_CANDIDATE,
        None,
        None,
    ),
    "incompatible language is skipped": (
        [candidate(2, 0.01, language="pt"), candidate(6, 0.1, language="EN-gb")],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        6,
        0.1,
    ),
    "a latest member beyond the time gap is skipped": (
        [candidate(2, 0.01, hours_ago=49), candidate(6, 0.1, hours_ago=47)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        6,
        0.1,
    ),
    "the time gap is inclusive and symmetric": (
        [candidate(2, 0.01, hours_ago=-48)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        2,
        0.01,
    ),
    "a near-identical Story months earlier": (
        [candidate(2, 0.06, hours_ago=24 * 150)],
        CREATE,
        MatchReason.NO_COMPATIBLE_CANDIDATE,
        None,
        None,
    ),
    "the ambiguous band ignores farther incompatible candidates": (
        [candidate(2, 0.05, hours_ago=100), candidate(3, 0.25)],
        CREATE,
        MatchReason.ABOVE_THRESHOLD,
        None,
        0.25,
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_decision_table(name):
    candidates, kind, reason, story_id, distance = CASES[name]

    decision = decide_story_match(ARTICLE, tuple(candidates), POLICY)

    assert decision == MatchDecision(
        kind=kind,
        reason=reason,
        story_id=story_id,
        distance=distance,
        candidate_count=len(candidates),
    )


def test_decision_does_not_depend_on_input_order():
    candidates = [candidate(story_id, 0.1 + story_id / 100) for story_id in range(1, 6)]
    expected = decide_story_match(ARTICLE, tuple(candidates), POLICY)
    for rotation in range(len(candidates)):
        rotated = tuple(candidates[rotation:] + candidates[:rotation])
        assert decide_story_match(ARTICLE, rotated, POLICY) == expected
    assert decide_story_match(ARTICLE, tuple(reversed(candidates)), POLICY) == expected


def test_distance_alone_is_insufficient_across_a_long_time_gap():
    months_earlier = (candidate(2, 0.06, hours_ago=24 * 150),)
    ignoring_time = MatchPolicy(max_distance=0.18, max_time_gap_hours=24 * 365)

    assert decide_story_match(ARTICLE, months_earlier, POLICY).kind is CREATE
    assert decide_story_match(ARTICLE, months_earlier, ignoring_time).kind is MATCH


def test_decision_inputs_carry_no_publication_identity():
    forbidden = {"content_fingerprint", "fingerprint", "duplicate_of", "source", "source_id"}
    snapshot_fields = {field.name for field in dataclasses.fields(ArticleMatchSnapshot)}
    candidate_fields = {field.name for field in dataclasses.fields(StoryCandidate)}

    assert snapshot_fields == {"article_id", "language", "event_time"}
    assert forbidden.isdisjoint(snapshot_fields | candidate_fields)
    assert list(inspect.signature(decide_story_match).parameters) == [
        "article",
        "candidates",
        "policy",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ARTICLE.language = "pt"


def test_decision_module_is_pure():
    for module in ("story_matching.py", "stories.py"):
        tree = ast.parse((Path(story_matching.__file__).parent / module).read_text())
        imported = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert not any(
            name.startswith(("django", "news.models", "news.application", "celery", "redis"))
            for name in imported
        ), (module, imported)


def test_matcher_key_names_the_policy_revision_and_every_threshold():
    key = POLICY.matcher_key
    assert key == "story-match-v1;max_distance=0.18;max_time_gap_hours=48.0"
    assert f"-v{POLICY_REVISION};" in key
    for field in dataclasses.fields(MatchPolicy):
        assert f"{field.name}=" in key, f"{field.name} must be part of matcher_key"
    assert len(key) <= 128  # StoryArticle.matcher_key max_length


@pytest.mark.parametrize(
    "changed",
    [
        {"max_distance": 0.19},
        {"max_distance": 0.179},
        {"max_distance": 0.1800001},
        {"max_time_gap_hours": 72},
        {"max_time_gap_hours": 48.5},
    ],
)
def test_changing_any_threshold_changes_matcher_key(changed):
    assert dataclasses.replace(POLICY, **changed).matcher_key != POLICY.matcher_key


def test_configured_thresholds_and_matcher_key_cannot_drift_apart():
    from news.application.story_matching import MATCH_POLICY, MATCHER_KEY

    assert MATCH_POLICY == MatchPolicy(
        max_distance=settings.NEWS_STORY_MATCH_MAX_DISTANCE,
        max_time_gap_hours=settings.NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS,
    )
    assert MATCHER_KEY == MATCH_POLICY.matcher_key == POLICY.matcher_key


@pytest.mark.parametrize(
    "fields", [{"max_distance": 0}, {"max_distance": 2.5}, {"max_time_gap_hours": 0}]
)
def test_policy_rejects_meaningless_thresholds(fields):
    with pytest.raises(ValueError):
        MatchPolicy(**({"max_distance": 0.18, "max_time_gap_hours": 48} | fields))


def _cosine_distance(left, right):
    dot = math.fsum(x * y for x, y in zip(left, right, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in left)) * math.sqrt(math.fsum(y * y for y in right))
    return 1 - dot / norm


def test_recorded_corpus_distances_support_the_chosen_threshold():
    """The margins the threshold comment in config/common.py relies on."""

    from tests.news.recorded_embeddings import RecordedEmbeddingProvider, corpus_inputs
    from tests.news.story_corpus import read_corpus

    ids = [article.id for article in read_corpus().articles]
    vectors = dict(zip(ids, RecordedEmbeddingProvider().embed(corpus_inputs()), strict=True))

    def distance(left, right):
        return _cosine_distance(vectors[left], vectors[right])

    threshold = settings.NEWS_STORY_MATCH_MAX_DISTANCE
    # The nearest different-event pair inside the time gap stays above it...
    lookalike = distance("kestrel-quake-01", "almen-quake-01")
    assert threshold < lookalike < 0.20
    assert lookalike - threshold > 0.01
    # ...while the reworded local report of the same earthquake stays below.
    assert distance("kestrel-quake-01", "kestrel-quake-02") < threshold
    # Identical syndicated text is identical evidence.
    assert distance("rail-strike-01", "rail-strike-02") < 1e-6
    # The reelection announcement is close in meaning to the budget vote;
    # only the time gap keeps it apart.
    assert distance("varrow-budget-01", "calloway-reelection-01") < threshold
    # The months-later storm is closer than any same-day report.
    assert distance("harbor-storm-01", "harbor-second-storm-01") < threshold
    # Known splits: same-event coverage just above the threshold.
    for left, right in (
        ("harbor-storm-01", "harbor-storm-03"),
        ("varrow-budget-01", "varrow-budget-02"),
        ("almen-flood-01", "almen-flood-02"),
    ):
        assert distance(left, right) > threshold
