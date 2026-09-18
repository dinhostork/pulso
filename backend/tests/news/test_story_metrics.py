"""Self-tests of the Story evaluation metrics on clusterings with known answers."""

import math

import pytest

from tests.news.story_corpus import read_corpus
from tests.news.story_metrics import evaluate


def assert_no_nan(report):
    for value in (
        report.precision,
        report.recall,
        report.false_merge_rate,
        report.false_split_rate,
    ):
        assert not math.isnan(value)
        assert 0.0 <= value <= 1.0


@pytest.fixture(scope="module")
def corpus_labels():
    """The real corpus's ground truth, keyed by position instead of database id."""

    return {
        position: article.expected_event
        for position, article in enumerate(read_corpus().articles, start=1)
    }


def perfect(labels):
    story_ids = {event: index for index, event in enumerate(sorted(set(labels.values())), 1)}
    return {article_id: story_ids[event] for article_id, event in labels.items()}


def test_perfect_clustering_of_the_corpus_scores_one(corpus_labels):
    report = evaluate(corpus_labels, perfect(corpus_labels))

    assert (report.precision, report.recall) == (1.0, 1.0)
    assert report.false_merge_count == report.false_split_count == 0
    assert report.false_merges == report.false_splits == ()
    assert report.unassigned_count == 0
    assert report.expected_pair_count == report.true_pair_count > 0
    assert_no_nan(report)


def test_one_story_for_everything_has_full_recall_and_false_merges(corpus_labels):
    report = evaluate(corpus_labels, dict.fromkeys(corpus_labels, 1))

    assert report.recall == 1.0
    assert report.false_split_count == 0
    assert report.false_merge_count > 0
    assert report.precision < 1.0
    assert math.isclose(report.false_merge_rate, 1.0 - report.precision)
    assert report.false_merge_count == report.predicted_pair_count - report.true_pair_count
    events = len(set(corpus_labels.values()))
    assert len(report.false_merges) == events * (events - 1) // 2
    assert_no_nan(report)


def test_every_article_alone_has_full_precision_and_false_splits(corpus_labels):
    report = evaluate(corpus_labels, {article_id: article_id for article_id in corpus_labels})

    assert report.precision == 1.0
    assert report.false_merge_count == 0
    assert report.false_split_count == report.expected_pair_count > 0
    assert report.recall == 0.0
    assert report.false_split_rate == 1.0
    split_events = {split.event for split in report.false_splits}
    multi_member = {
        event for event in corpus_labels.values() if list(corpus_labels.values()).count(event) > 1
    }
    assert split_events == multi_member
    assert_no_nan(report)


def test_worked_example_counts_and_rates():
    expected = {1: "flood", 2: "flood", 3: "flood", 4: "strike", 5: "strike"}
    assigned = {1: 10, 2: 10, 3: 11, 4: 11, 5: None}

    report = evaluate(expected, assigned)

    # Expected pairs: 1-2, 1-3, 2-3, 4-5. Predicted pairs: 1-2, 3-4.
    assert (report.expected_pair_count, report.predicted_pair_count) == (4, 2)
    assert report.true_pair_count == 1
    assert (report.precision, report.recall) == (0.5, 0.25)
    assert (report.false_merge_count, report.false_merge_rate) == (1, 0.5)
    # 1-3 and 2-3 are split; 4-5 is missed because 5 is unassigned, not split.
    assert (report.false_split_count, report.false_split_rate) == (2, 0.5)
    assert report.unassigned_article_ids == (5,)


def test_false_merge_report_names_both_events_the_story_and_the_articles():
    report = evaluate(
        {1: "flood", 2: "flood", 3: "inquiry"},
        {1: 7, 2: 7, 3: 7},
        names={1: "almen-flood-01", 2: "almen-flood-02", 3: "almen-inquiry-01"},
    )

    (merge,) = report.false_merges
    assert merge.story_id == 7
    assert merge.events == ("flood", "inquiry")
    assert merge.article_ids == ((1, 2), (3,))
    assert merge.pair_count == 2
    text = report.describe()
    assert "FALSE MERGE story 7 joins 'flood'" in text
    assert "'inquiry'" in text and "3 (almen-inquiry-01)" in text


def test_false_split_report_names_the_event_and_every_story():
    report = evaluate({1: "flood", 2: "flood", 3: "flood"}, {1: 4, 2: 9, 3: 9})

    (split,) = report.false_splits
    assert split.event == "flood"
    assert split.story_ids == (4, 9)
    assert split.article_ids_by_story == ((4, (1,)), (9, (2, 3)))
    assert split.pair_count == 2
    assert "FALSE SPLIT 'flood' across stories [4, 9]" in report.describe()


def test_zero_denominators_are_defined_and_never_nan():
    empty = evaluate({}, {})
    assert (empty.precision, empty.recall) == (1.0, 1.0)
    assert (empty.false_merge_rate, empty.false_split_rate) == (0.0, 0.0)

    distinct = evaluate({1: "a", 2: "b"}, {1: 1, 2: 2})
    assert (distinct.expected_pair_count, distinct.predicted_pair_count) == (0, 0)
    assert (distinct.precision, distinct.recall) == (1.0, 1.0)

    unassigned = evaluate({1: "a", 2: "a"}, {})
    assert (unassigned.precision, unassigned.recall) == (1.0, 0.0)
    assert unassigned.false_split_count == 0
    assert unassigned.unassigned_article_ids == (1, 2)
    for report in (empty, distinct, unassigned):
        assert_no_nan(report)


def test_assignment_outside_the_ground_truth_is_rejected():
    with pytest.raises(ValueError, match="without an expected event"):
        evaluate({1: "a"}, {1: 1, 2: 1})
