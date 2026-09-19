"""Pure Story matching decision (#28, #36, #38): table-driven rules, the secondary
verifier's boundaries, policy identity and the threshold evidence."""

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
    MAX_MATCHER_KEY_LENGTH,
    POLICY_REVISION,
    ArticleMatchSnapshot,
    CandidateEvidence,
    MatchKind,
    MatchPolicy,
    MatchReason,
    MatchRule,
    VerificationResult,
    decide_story_match,
    secondary_candidates,
)

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
POLICY = MatchPolicy(
    max_distance=0.18,
    max_time_gap_hours=48,
    secondary_max_distance=0.25,
    secondary_max_member_distance=0.22,
    min_anchors=1,
    max_members=20,
)
ARTICLE = ArticleMatchSnapshot(article_id=1, language="en", event_time=NOW)


def candidate(
    story_id, distance, *, hours_ago=1, first_hours_ago=None, language="en", status=STORY_ACTIVE
):
    return StoryCandidate(
        story_id=story_id,
        distance=distance,
        member_count=1,
        last_article_published_at=NOW - timedelta(hours=hours_ago),
        language=language,
        status=status,
        first_article_published_at=(
            None if first_hours_ago is None else NOW - timedelta(hours=first_hours_ago)
        ),
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
    "the secondary band without verified evidence creates a Story": (
        [candidate(4, 0.181), candidate(5, 0.45)],
        CREATE,
        MatchReason.VERIFICATION_REJECTED,
        None,
        0.181,
    ),
    "beyond the secondary band creates a Story unverified": (
        [candidate(4, 0.251), candidate(5, 0.45)],
        CREATE,
        MatchReason.ABOVE_THRESHOLD,
        None,
        0.251,
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
    "the secondary band ignores nearer incompatible candidates": (
        [candidate(2, 0.05, hours_ago=100), candidate(3, 0.25)],
        CREATE,
        MatchReason.VERIFICATION_REJECTED,
        None,
        0.25,
    ),
    "an Article inside a longer Story's member range is compatible": (
        [candidate(2, 0.1, hours_ago=-72, first_hours_ago=0)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        2,
        0.1,
    ),
    "the gap to a member range is measured to its nearest end": (
        [candidate(2, 0.1, hours_ago=-120, first_hours_ago=-48)],
        MATCH,
        MatchReason.WITHIN_THRESHOLD,
        2,
        0.1,
    ),
    "a member range starting beyond the gap is incompatible": (
        [candidate(2, 0.1, hours_ago=-120, first_hours_ago=-49)],
        CREATE,
        MatchReason.NO_COMPATIBLE_CANDIDATE,
        None,
        None,
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_decision_table(name):
    candidates, kind, reason, story_id, distance = CASES[name]

    decision = decide_story_match(ARTICLE, tuple(candidates), POLICY)

    assert (decision.kind, decision.reason, decision.story_id, decision.distance) == (
        kind,
        reason,
        story_id,
        distance,
    )
    assert decision.candidate_count == len(candidates)
    assert decision.rule == (MatchRule.PRIMARY_DISTANCE if kind is MATCH else None)


def test_decision_does_not_depend_on_input_order():
    candidates = [candidate(story_id, 0.1 + story_id / 100) for story_id in range(1, 6)]
    expected = decide_story_match(ARTICLE, tuple(candidates), POLICY)
    for rotation in range(len(candidates)):
        rotated = tuple(candidates[rotation:] + candidates[:rotation])
        assert decide_story_match(ARTICLE, rotated, POLICY) == expected
    assert decide_story_match(ARTICLE, tuple(reversed(candidates)), POLICY) == expected


def test_distance_alone_is_insufficient_across_a_long_time_gap():
    months_earlier = (candidate(2, 0.06, hours_ago=24 * 150),)
    ignoring_time = dataclasses.replace(POLICY, max_time_gap_hours=24 * 365)

    assert decide_story_match(ARTICLE, months_earlier, POLICY).kind is CREATE
    assert decide_story_match(ARTICLE, months_earlier, ignoring_time).kind is MATCH


def test_decision_inputs_carry_no_publication_identity():
    forbidden = {"content_fingerprint", "fingerprint", "duplicate_of", "source", "source_id"}
    snapshot_fields = {field.name for field in dataclasses.fields(ArticleMatchSnapshot)}
    candidate_fields = {field.name for field in dataclasses.fields(StoryCandidate)}
    evidence_fields = {field.name for field in dataclasses.fields(CandidateEvidence)}

    assert snapshot_fields == {"article_id", "language", "event_time"}
    # Secondary evidence is numbers only: no text, no Topic/Entity, no Source.
    assert evidence_fields == {"nearest_member_distance", "members_checked", "shared_anchors"}
    assert forbidden.isdisjoint(snapshot_fields | candidate_fields | evidence_fields)
    assert list(inspect.signature(decide_story_match).parameters) == [
        "article",
        "candidates",
        "policy",
        "evidence",
        # A Story id (#38), the assignment being re-decided; not publication identity.
        "current_story_id",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ARTICLE.language = "pt"


def test_decision_module_is_pure():
    for module in ("story_matching.py", "stories.py", "event_anchors.py"):
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
    from news.models import ArticleStoryProcessing, StoryArticle

    key = POLICY.matcher_key
    assert key == (
        "story-match-v3;max_distance=0.18;max_time_gap_hours=48.0;"
        "secondary_max_distance=0.25;secondary_max_member_distance=0.22;"
        "min_anchors=1;max_members=20"
    )
    assert f"-v{POLICY_REVISION};" in key
    for field in dataclasses.fields(MatchPolicy):
        assert f"{field.name}=" in key, f"{field.name} must be part of matcher_key"
    for model in (StoryArticle, ArticleStoryProcessing):
        assert model._meta.get_field("matcher_key").max_length == MAX_MATCHER_KEY_LENGTH
    # Room for longer threshold reprs (0.1825, 47.5) and another bound.
    assert len(key) <= MAX_MATCHER_KEY_LENGTH - 64


def test_a_policy_whose_key_would_not_fit_its_columns_is_refused(monkeypatch):
    monkeypatch.setattr(story_matching, "POLICY_NAME", "x" * MAX_MATCHER_KEY_LENGTH)

    with pytest.raises(ValueError, match="matcher_key"):
        dataclasses.replace(POLICY)


@pytest.mark.parametrize(
    "changed",
    [
        {"max_distance": 0.19},
        {"max_distance": 0.179},
        {"max_distance": 0.1800001},
        {"max_time_gap_hours": 72},
        {"max_time_gap_hours": 48.5},
        {"secondary_max_distance": 0.26},
        {"secondary_max_member_distance": 0.21},
        {"secondary_max_member_distance": 0.2200001},
        {"min_anchors": 2},
        {"max_members": 10},
    ],
)
def test_changing_any_threshold_changes_matcher_key(changed):
    assert dataclasses.replace(POLICY, **changed).matcher_key != POLICY.matcher_key


def test_configured_thresholds_and_matcher_key_cannot_drift_apart():
    from news.application.story_matching import MATCH_POLICY, MATCHER_KEY

    assert MATCH_POLICY == MatchPolicy(
        max_distance=settings.NEWS_STORY_MATCH_MAX_DISTANCE,
        max_time_gap_hours=settings.NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS,
        secondary_max_distance=settings.NEWS_STORY_MATCH_SECONDARY_MAX_DISTANCE,
        secondary_max_member_distance=settings.NEWS_STORY_MATCH_SECONDARY_MAX_MEMBER_DISTANCE,
        min_anchors=1,
        max_members=settings.NEWS_STORY_MATCH_VERIFY_MAX_MEMBERS,
    )
    assert MATCHER_KEY == MATCH_POLICY.matcher_key == POLICY.matcher_key


@pytest.mark.parametrize(
    "fields",
    [
        {"max_distance": 0},
        {"max_distance": 2.5},
        {"max_time_gap_hours": 0},
        {"secondary_max_distance": 0.18},
        {"secondary_max_distance": 0.1},
        {"secondary_max_distance": 2.1},
        # The member bound lies in (0, secondary_max_distance]: a member farther
        # than the candidate band could never be the evidence that confirms it.
        {"secondary_max_member_distance": 0},
        {"secondary_max_member_distance": -0.1},
        {"secondary_max_member_distance": 0.2500001},
        {"min_anchors": 0},
        {"max_members": 0},
    ],
)
def test_policy_rejects_meaningless_thresholds(fields):
    with pytest.raises(ValueError):
        dataclasses.replace(POLICY, **fields)


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
    # Revision 1's known splits: same-event coverage just above the primary
    # threshold, inside the secondary bound, which the verifier now decides.
    secondary = settings.NEWS_STORY_MATCH_SECONDARY_MAX_DISTANCE
    for left, right in (
        ("harbor-storm-01", "harbor-storm-03"),
        ("varrow-budget-01", "varrow-budget-02"),
        ("almen-flood-01", "almen-flood-02"),
        ("almen-flood-02", "almen-flood-03"),
    ):
        assert threshold < distance(left, right) <= secondary - 0.03
    # The lookalike earthquake is inside the secondary bound too: only the
    # names can reject it (test_event_anchors.py).
    assert lookalike < secondary
    # The dam inquiry shares the river's name; no flood report is within the
    # secondary bound of it, which is what rejects it.
    for flood in ("almen-flood-01", "almen-flood-02", "almen-flood-03"):
        assert distance(flood, "almen-inquiry-01") > secondary + 0.015

    # Revision 3 (#38): the member bound sits between the farthest same-event
    # nearest member and the nearest different-event member sharing a name.
    member_bound = settings.NEWS_STORY_MATCH_SECONDARY_MAX_MEMBER_DISTANCE
    same_event_members = {
        "varrow-budget-02": distance("varrow-budget-01", "varrow-budget-02"),
        "harbor-storm-03": distance("harbor-storm-02", "harbor-storm-03"),
        "almen-flood-02": distance("almen-flood-01", "almen-flood-02"),
        "almen-flood-03": distance("almen-flood-02", "almen-flood-03"),
    }
    farthest_same_event = max(same_event_members.values())
    assert farthest_same_event == pytest.approx(0.2152, abs=1e-4)
    # The two real-world false-merge classes, synthetic, each sharing one name.
    hard_negatives = {
        "same conflict": distance("dunmar-collapse-01", "sarran-strikes-01"),
        "same war and weapon": distance("tarvia-drone-warning-01", "tarvia-delegation-drones-01"),
    }
    nearest_hard_negative = min(hard_negatives.values())
    assert nearest_hard_negative == pytest.approx(0.2267, abs=1e-4)
    assert farthest_same_event < member_bound < nearest_hard_negative
    assert member_bound - farthest_same_event > 0.004
    assert nearest_hard_negative - member_bound > 0.006
    # Both reach the secondary band, so the verifier, not retrieval or the
    # candidate bound, is what separates them; revision 2 accepted both.
    for pair, value in hard_negatives.items():
        assert threshold < value <= secondary, pair
    # The candidate bound cannot drop to the member bound: the third flood
    # report is 0.238 from the first, which alone formed the Story's vector,
    # but 0.211 from the second, its nearest member.
    assert distance("almen-flood-01", "almen-flood-03") > member_bound


# --- the secondary verifier (#36) ------------------------------------------------------


def evidence(member=0.2, anchors=1, checked=3):
    return CandidateEvidence(
        nearest_member_distance=member, members_checked=checked, shared_anchors=anchors
    )


def test_below_the_primary_threshold_nothing_is_verified():
    near = (candidate(3, 0.17), candidate(4, 0.2))

    decision = decide_story_match(ARTICLE, near, POLICY, {4: evidence()})

    assert (decision.kind, decision.rule, decision.story_id) == (
        MATCH,
        MatchRule.PRIMARY_DISTANCE,
        3,
    )
    assert decision.verifications == ()
    assert secondary_candidates(ARTICLE, near, POLICY) == ()


def test_just_above_the_threshold_with_same_event_evidence_is_a_secondary_match():
    decision = decide_story_match(ARTICLE, (candidate(4, 0.189),), POLICY, {4: evidence(0.189, 1)})

    assert decision.kind is MATCH and decision.story_id == 4
    assert decision.reason is MatchReason.VERIFIED_SAME_EVENT
    assert decision.rule is MatchRule.SECONDARY_EVENT_VERIFY
    assert decision.distance == 0.189
    (check,) = decision.verifications
    assert (check.story_id, check.result) == (4, VerificationResult.ACCEPTED)
    assert (check.nearest_member_distance, check.members_checked, check.shared_anchors) == (
        0.189,
        3,
        1,
    )


@pytest.mark.parametrize(
    ("given", "result"),
    [
        (evidence(anchors=0), VerificationResult.NO_SHARED_ANCHOR),
        (evidence(member=0.26), VerificationResult.MEMBER_TOO_FAR),
        (evidence(member=0.23), VerificationResult.MEMBER_TOO_FAR),
        (evidence(member=None), VerificationResult.NO_MEMBER_EVIDENCE),
        (None, VerificationResult.NO_MEMBER_EVIDENCE),
    ],
)
def test_insufficient_evidence_is_rejected(given, result):
    decision = decide_story_match(
        ARTICLE, (candidate(4, 0.2),), POLICY, {} if given is None else {4: given}
    )

    assert decision.kind is CREATE and decision.reason is MatchReason.VERIFICATION_REJECTED
    assert decision.distance == 0.2 and decision.rule is None
    assert [check.result for check in decision.verifications] == [result]


def test_a_templated_lookalike_without_a_shared_name_is_rejected():
    """The earthquake pair: 0.192 apart, 15 minutes apart, different places."""

    lookalike = (candidate(5, 0.1919, hours_ago=0.25),)

    decision = decide_story_match(ARTICLE, lookalike, POLICY, {5: evidence(0.1919, 0)})

    assert decision.kind is CREATE
    assert decision.verifications[0].result is VerificationResult.NO_SHARED_ANCHOR


def test_a_centroid_pulled_towards_a_report_no_member_resembles_is_rejected():
    """The dam inquiry: the Story vector is 0.23 away, its nearest member 0.268."""

    decision = decide_story_match(ARTICLE, (candidate(9, 0.23),), POLICY, {9: evidence(0.268, 1)})

    assert decision.kind is CREATE
    assert decision.verifications[0].result is VerificationResult.MEMBER_TOO_FAR


def test_time_incompatible_candidates_are_never_verified():
    months_earlier = (candidate(2, 0.2, hours_ago=24 * 150),)
    strong = {2: evidence(0.19, 5)}

    assert secondary_candidates(ARTICLE, months_earlier, POLICY) == ()
    decision = decide_story_match(ARTICLE, months_earlier, POLICY, strong)
    assert decision.reason is MatchReason.NO_COMPATIBLE_CANDIDATE
    assert decision.verifications == ()


def test_the_nearest_verified_candidate_wins_then_the_lower_story_id():
    ordered = (candidate(8, 0.21), candidate(7, 0.2), candidate(6, 0.21))
    all_pass = {story_id: evidence() for story_id in (6, 7, 8)}

    assert decide_story_match(ARTICLE, ordered, POLICY, all_pass).story_id == 7
    tie = (candidate(8, 0.21), candidate(6, 0.21))
    assert decide_story_match(ARTICLE, tie, POLICY, all_pass).story_id == 6
    for rotation in (tie, tuple(reversed(tie))):
        assert decide_story_match(ARTICLE, rotation, POLICY, all_pass).story_id == 6


def test_a_rejected_nearer_candidate_does_not_hide_a_verified_farther_one():
    two = (candidate(3, 0.19), candidate(4, 0.22))
    given = {3: evidence(anchors=0), 4: evidence(0.22, 2)}

    decision = decide_story_match(ARTICLE, two, POLICY, given)

    assert (decision.kind, decision.story_id, decision.distance) == (MATCH, 4, 0.22)
    assert [(c.story_id, c.result) for c in decision.verifications] == [
        (3, VerificationResult.NO_SHARED_ANCHOR),
        (4, VerificationResult.ACCEPTED),
    ]


def test_secondary_bounds_are_inclusive():
    at_bound = (candidate(4, 0.25),)

    accepted = decide_story_match(ARTICLE, at_bound, POLICY, {4: evidence(0.22, 1)})
    assert accepted.kind is MATCH and accepted.rule is MatchRule.SECONDARY_EVENT_VERIFY

    beyond = decide_story_match(ARTICLE, (candidate(4, 0.2500001),), POLICY, {4: evidence()})
    assert beyond.reason is MatchReason.ABOVE_THRESHOLD and beyond.verifications == ()
    member_beyond = decide_story_match(ARTICLE, at_bound, POLICY, {4: evidence(0.2200001, 1)})
    assert member_beyond.verifications[0].result is VerificationResult.MEMBER_TOO_FAR
    stricter = dataclasses.replace(POLICY, min_anchors=2)
    assert decide_story_match(ARTICLE, at_bound, stricter, {4: evidence(0.2, 2)}).kind is MATCH
    assert decide_story_match(ARTICLE, at_bound, stricter, {4: evidence(0.2, 1)}).kind is CREATE


def test_the_anchor_rule_only_applies_to_languages_it_is_defined_for():
    portuguese = ArticleMatchSnapshot(article_id=1, language="pt-BR", event_time=NOW)

    decision = decide_story_match(
        portuguese, (candidate(4, 0.2, language="pt"),), POLICY, {4: evidence(0.2, 3)}
    )

    assert decision.kind is CREATE
    assert decision.verifications[0].result is VerificationResult.LANGUAGE_UNSUPPORTED


# --- member evidence apart from candidate distance (#38) -------------------------------


def test_candidate_distance_and_member_distance_are_judged_independently():
    # A drifted Story vector far out in the band, one member a close report:
    # the candidate bound admits it and the member bound confirms it.
    drifted = decide_story_match(ARTICLE, (candidate(4, 0.241),), POLICY, {4: evidence(0.204, 2)})
    assert (drifted.kind, drifted.story_id) == (MATCH, 4)
    assert drifted.verifications[0].result is VerificationResult.ACCEPTED

    # A Story vector just above the primary threshold does not excuse a member
    # that only resembles the report at the top of the band.
    near_vector = decide_story_match(ARTICLE, (candidate(4, 0.19),), POLICY, {4: evidence(0.23, 3)})
    assert near_vector.kind is CREATE
    assert near_vector.verifications[0].result is VerificationResult.MEMBER_TOO_FAR
    assert near_vector.verifications[0].nearest_member_distance == 0.23


REVISION_TWO_MEMBER_BOUND = dataclasses.replace(POLICY, secondary_max_member_distance=0.25)


@pytest.mark.parametrize(
    ("label", "story_distance", "member_distance", "anchors", "joined"),
    [
        # The real-world smoke test (#38), recorded as numbers only.
        ("real-world conflict false merge", 0.243111, 0.243111, 1, False),
        ("real-world drone false merge", 0.245669, 0.245669, 1, False),
        ("real-world same-proposal positive control", 0.213153, 0.213153, 3, True),
        # Their synthetic corpus equivalents and the corpus's one-anchor match.
        ("synthetic same-conflict hard negative", 0.236014, 0.236014, 1, False),
        ("synthetic same-war hard negative", 0.226660, 0.226660, 1, False),
        ("varrow budget, one shared name", 0.188946, 0.188946, 1, True),
        ("almen flood, drifted Story vector", 0.238190, 0.210872, 2, True),
    ],
)
def test_the_member_bound_separates_the_measured_cases(
    label, story_distance, member_distance, anchors, joined
):
    candidates = (candidate(4, story_distance),)
    given = {4: evidence(member_distance, anchors)}

    decision = decide_story_match(ARTICLE, candidates, POLICY, given)

    assert (decision.kind is MATCH) is joined, label
    if not joined:
        assert decision.verifications[0].result is VerificationResult.MEMBER_TOO_FAR, label
        # Under revision 2's rule (one 0.25 bound) every one of them was joined.
        before = decide_story_match(ARTICLE, candidates, REVISION_TWO_MEMBER_BOUND, given)
        assert before.kind is MATCH, label


# --- re-deciding a stale assignment (#38) ----------------------------------------------


def test_a_stale_assignment_stays_in_its_story_while_the_rule_still_accepts_it():
    """The flood chain: the report sits between two Stories of one event and
    the nearer one would strand the Story it is leaving."""

    two = (candidate(8, 0.2109), candidate(9, 0.2152))
    given = {8: evidence(0.2109, 2), 9: evidence(0.2152, 1)}

    assert decide_story_match(ARTICLE, two, POLICY, given).story_id == 8
    kept = decide_story_match(ARTICLE, two, POLICY, given, current_story_id=9)

    assert (kept.kind, kept.story_id, kept.kept_current_story) == (MATCH, 9, True)
    assert kept.rule is MatchRule.SECONDARY_EVENT_VERIFY
    assert kept.distance == 0.2152 and kept.candidate_count == 2
    assert [check.story_id for check in kept.verifications] == [9]


def test_a_stale_assignment_the_rule_rejects_is_decided_afresh():
    """The revision 2 false merge: the Story it is leaving, rebuilt without it,
    is in the band but its nearest member is beyond the member bound."""

    merged = (candidate(12, 0.2144),)
    given = {12: evidence(0.2360, 1)}

    decision = decide_story_match(ARTICLE, merged, POLICY, given, current_story_id=12)

    assert decision.kind is CREATE and decision.kept_current_story is False
    assert decision.reason is MatchReason.VERIFICATION_REJECTED
    assert [check.result for check in decision.verifications] == [VerificationResult.MEMBER_TOO_FAR]


def test_the_current_story_is_verified_even_when_another_is_a_primary_match():
    near = (candidate(3, 0.15), candidate(9, 0.2))

    assert secondary_candidates(ARTICLE, near, POLICY) == ()
    assert [c.story_id for c in secondary_candidates(ARTICLE, near, POLICY, 9)] == [9]
    kept = decide_story_match(ARTICLE, near, POLICY, {9: evidence(0.2, 1)}, current_story_id=9)
    assert (kept.story_id, kept.kept_current_story) == (9, True)
    # Without its evidence the current Story is not kept.
    assert decide_story_match(ARTICLE, near, POLICY, {}, current_story_id=9).story_id == 3


def test_a_current_story_that_is_not_a_compatible_candidate_changes_nothing():
    near = (candidate(3, 0.15), candidate(9, 0.1, status=STORY_ARCHIVED))

    for current in (9, 42, None):
        decision = decide_story_match(ARTICLE, near, POLICY, {}, current_story_id=current)
        assert (decision.story_id, decision.kept_current_story) == (3, False)
