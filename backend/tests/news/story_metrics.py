"""Score an Article -> Story assignment against expected event labels.

This module measures; it never matches. It takes ground truth
(`{article_id: expected_event}`) and an assignment produced elsewhere
(`{article_id: story_id or None}`) and compares them over the pairwise
same-event relation, following the usual pair-counting definitions:

- E, expected pairs: unordered pairs of distinct Articles with the same
  `expected_event`.
- P, predicted pairs: unordered pairs of distinct Articles that are both
  assigned and share one Story id.
- precision = |E ∩ P| / |P|;  recall = |E ∩ P| / |E|.
- false merge pairs = P \\ E (joined but different events);
  false merge rate = false merges / |P|, which equals 1 - precision.
- false split pairs = pairs in E where both Articles are assigned but to
  different Stories; false split rate = false splits / |E|.
- unassigned: Articles with no Story (missing or None). Their expected pairs
  count against recall but are not false splits, so a matcher that skips
  work is reported separately from one that fragments events.

Zero denominators are defined, never NaN: with no predicted pairs, precision
is 1.0 and the false merge rate 0.0 (nothing was wrongly joined); with no
expected pairs, recall is 1.0 and the false split rate 0.0 (nothing could be
split).

Every false merge and false split is reported by name, grouped by Story and
event pair (merges) or by event (splits), so a failing quality gate is
diagnosable from CI output alone via `EvaluationReport.describe()`.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

from news.models import StoryArticle


@dataclass(frozen=True)
class FalseMerge:
    """One Story that joins Articles of two different expected events."""

    story_id: int
    events: tuple[str, str]
    article_ids: tuple[tuple[int, ...], tuple[int, ...]]
    pair_count: int


@dataclass(frozen=True)
class FalseSplit:
    """One expected event spread across more than one Story."""

    event: str
    story_ids: tuple[int, ...]
    article_ids_by_story: tuple[tuple[int, tuple[int, ...]], ...]
    pair_count: int


@dataclass(frozen=True)
class EvaluationReport:
    precision: float
    recall: float
    expected_pair_count: int
    predicted_pair_count: int
    true_pair_count: int
    false_merge_count: int
    false_merge_rate: float
    false_split_count: int
    false_split_rate: float
    unassigned_article_ids: tuple[int, ...]
    false_merges: tuple[FalseMerge, ...]
    false_splits: tuple[FalseSplit, ...]
    names: Mapping[int, str]

    @property
    def unassigned_count(self) -> int:
        return len(self.unassigned_article_ids)

    def _name(self, article_id: int) -> str:
        name = self.names.get(article_id)
        return f"{article_id} ({name})" if name else str(article_id)

    def _names(self, article_ids: Iterable[int]) -> str:
        return ", ".join(self._name(article_id) for article_id in article_ids)

    def describe(self) -> str:
        """Aggregate scores plus every offending pair group, for assertion messages."""

        lines = [
            f"precision={self.precision:.4f} recall={self.recall:.4f} "
            f"false_merges={self.false_merge_count} (rate {self.false_merge_rate:.4f}) "
            f"false_splits={self.false_split_count} (rate {self.false_split_rate:.4f}) "
            f"unassigned={self.unassigned_count}"
        ]
        for merge in self.false_merges:
            left, right = merge.events
            lines.append(
                f"FALSE MERGE story {merge.story_id} joins {left!r} [{self._names(merge.article_ids[0])}]"
                f" with {right!r} [{self._names(merge.article_ids[1])}]: {merge.pair_count} pairs"
            )
        for split in self.false_splits:
            parts = "; ".join(
                f"story {story_id}: [{self._names(article_ids)}]"
                for story_id, article_ids in split.article_ids_by_story
            )
            lines.append(
                f"FALSE SPLIT {split.event!r} across stories {list(split.story_ids)} — {parts}:"
                f" {split.pair_count} pairs"
            )
        if self.unassigned_article_ids:
            lines.append(f"UNASSIGNED {self._names(self.unassigned_article_ids)}")
        return "\n".join(lines)


def _ratio(numerator: int, denominator: int, *, empty: float) -> float:
    return numerator / denominator if denominator else empty


def evaluate(
    expected: Mapping[int, str],
    assigned: Mapping[int, int | None],
    *,
    names: Mapping[int, str] | None = None,
) -> EvaluationReport:
    """Score `assigned` against `expected`; both keyed by Article id."""

    stray = sorted(set(assigned) - set(expected))
    if stray:
        raise ValueError(f"assignment includes Articles without an expected event: {stray}")
    article_ids = sorted(expected)
    story_of = {article_id: assigned.get(article_id) for article_id in article_ids}

    expected_pairs = true_pairs = predicted_pairs = split_pairs = 0
    merges: dict[tuple[int, str, str], int] = defaultdict(int)
    splits: dict[str, int] = defaultdict(int)
    for left, right in combinations(article_ids, 2):
        same_event = expected[left] == expected[right]
        left_story, right_story = story_of[left], story_of[right]
        both_assigned = left_story is not None and right_story is not None
        same_story = both_assigned and left_story == right_story
        expected_pairs += same_event
        predicted_pairs += same_story
        true_pairs += same_event and same_story
        if same_story and not same_event:
            first, second = sorted((expected[left], expected[right]))
            merges[(left_story, first, second)] += 1
        if same_event and both_assigned and not same_story:
            split_pairs += 1
            splits[expected[left]] += 1

    members: dict[tuple[int, str], list[int]] = defaultdict(list)
    for article_id in article_ids:
        if story_of[article_id] is not None:
            members[(story_of[article_id], expected[article_id])].append(article_id)

    false_merges = tuple(
        FalseMerge(
            story_id=story_id,
            events=(first, second),
            article_ids=(tuple(members[(story_id, first)]), tuple(members[(story_id, second)])),
            pair_count=count,
        )
        for (story_id, first, second), count in sorted(merges.items())
    )
    false_splits = []
    for event, count in sorted(splits.items()):
        by_story = sorted(
            (story_id, tuple(ids)) for (story_id, label), ids in members.items() if label == event
        )
        false_splits.append(
            FalseSplit(
                event=event,
                story_ids=tuple(story_id for story_id, _ in by_story),
                article_ids_by_story=tuple(by_story),
                pair_count=count,
            )
        )
    false_merge_count = sum(merges.values())
    return EvaluationReport(
        precision=_ratio(true_pairs, predicted_pairs, empty=1.0),
        recall=_ratio(true_pairs, expected_pairs, empty=1.0),
        expected_pair_count=expected_pairs,
        predicted_pair_count=predicted_pairs,
        true_pair_count=true_pairs,
        false_merge_count=false_merge_count,
        false_merge_rate=_ratio(false_merge_count, predicted_pairs, empty=0.0),
        false_split_count=split_pairs,
        false_split_rate=_ratio(split_pairs, expected_pairs, empty=0.0),
        unassigned_article_ids=tuple(
            article_id for article_id in article_ids if story_of[article_id] is None
        ),
        false_merges=false_merges,
        false_splits=tuple(false_splits),
        names=dict(names or {}),
    )


def primary_assignments(article_ids: Iterable[int]) -> dict[int, int | None]:
    """Read each Article's primary Story from `StoryArticle`; None when it has none."""

    wanted = list(article_ids)
    primary = dict(
        StoryArticle.objects.filter(article_id__in=wanted, is_primary=True).values_list(
            "article_id", "story_id"
        )
    )
    return {article_id: primary.get(article_id) for article_id in wanted}
