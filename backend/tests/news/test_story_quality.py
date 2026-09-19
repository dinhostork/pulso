"""Full match + refresh quality on the repository-owned synthetic regression corpus.

The direct matcher gate lives in test_story_matching_corpus.py. Both use the
existing #26 evaluator, recorded local embeddings and the same 32 Articles /
16 events / 21 same-event pairs. These are not production accuracy estimates.

Historical, on the 26-Article corpus (12 events, 19 pairs): matcher v1 (#28)
precision 1.000, recall 0.632 (12/19), zero false merges, seven false-split
pairs; pre-#36 full match + refresh precision 1.000, recall 0.789 (15/19), four
false-split pairs (Varrow and Almen flood); matcher v2 (#36) 1.000 / 1.000.

#38 added the same-conflict and same-war hard negatives (6 Articles, 4 events,
2 pairs). Matcher v2 on the expanded corpus, full match + refresh: precision
0.840, recall 1.000, four false-merge pairs (both hard negatives merged).
Matcher v3 (#38) groups all 16 events correctly in both modes: precision and
recall 1.000, zero false merges/splits/unassigned Articles.
"""

import pytest

from tests.news.story_corpus import load_corpus
from tests.news.story_metrics import evaluate, primary_assignments
from tests.news.story_pipeline import (
    grouping_diagnostics,
    publication_order,
    run_pipeline,
    use_recorded_provider,
)

# No tolerance: the repository-owned deterministic corpus currently groups all
# 16 events correctly; any new merge or split is a deliberate behavior change
# that must be re-measured, not silently tolerated.
EXPECTED_PRECISION = 1.0
EXPECTED_RECALL = 1.0
EXPECTED_FALSE_MERGES = 0
EXPECTED_FALSE_MERGE_RATE = 0.0
EXPECTED_FALSE_SPLITS = 0
EXPECTED_FALSE_SPLIT_RATE = 0.0
EXPECTED_UNASSIGNED = 0


@pytest.fixture
def report(db, monkeypatch):
    use_recorded_provider(monkeypatch)
    loaded = load_corpus()
    run_pipeline(publication_order(loaded))
    result = evaluate(
        loaded.expected_events,
        primary_assignments(loaded.article_ids.values()),
        names=loaded.names,
    )
    # Printed with -s and on failure: every offending pair, label and Story id.
    print(grouping_diagnostics(loaded))
    return result


@pytest.mark.django_db
def test_corpus_size_is_the_measured_one(report):
    assert report.expected_pair_count == 21, report.describe()
    assert len(report.names) == 32


@pytest.mark.django_db
def test_no_false_merge(report):
    assert report.false_merge_count == EXPECTED_FALSE_MERGES, report.describe()
    assert report.false_merge_rate == EXPECTED_FALSE_MERGE_RATE, report.describe()
    assert not report.false_merges, report.describe()


@pytest.mark.django_db
def test_precision(report):
    assert report.precision == EXPECTED_PRECISION, report.describe()


@pytest.mark.django_db
def test_recall(report):
    assert report.recall == EXPECTED_RECALL, report.describe()


@pytest.mark.django_db
def test_no_false_split(report):
    assert report.false_split_count == EXPECTED_FALSE_SPLITS, report.describe()
    assert report.false_split_rate == EXPECTED_FALSE_SPLIT_RATE, report.describe()
    assert not report.false_splits, report.describe()


@pytest.mark.django_db
def test_every_article_is_assigned(report):
    assert report.unassigned_count == EXPECTED_UNASSIGNED, report.describe()
