"""Pure decision-table tests for ADR-0010.

No database, no Django settings and no ORM model takes part: the decision is a
function of one normalized revision and up to three candidate snapshots.
"""

import ast
import dataclasses
import pathlib

import pytest

from news.domain import dedup
from news.domain.dedup import (
    DEFAULT_MIN_FINGERPRINT_CHARS,
    ArticleMatch,
    DecisionKind,
    decide,
)
from news.domain.normalization import NormalizedArticle

SOURCE = 1
OTHER_SOURCE = 2
URL = "https://news.example/story/1"
OTHER_URL = "https://news.example/story/2"
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


def normalized(
    *,
    canonical_url=URL,
    fingerprint=FINGERPRINT,
    external_id="story-1",
    length=DEFAULT_MIN_FINGERPRINT_CHARS,
):
    return NormalizedArticle(
        external_id=external_id,
        canonical_url=canonical_url,
        title="Story 1",
        description="",
        body_text="Body 1",
        byline="",
        published_at=None,
        language="en",
        content_fingerprint=fingerprint,
        fingerprint_input_length=length,
    )


def match(article_id=10, *, source_id=SOURCE, canonical_url=URL, content_fingerprint=FINGERPRINT):
    return ArticleMatch(
        id=article_id,
        source_id=source_id,
        canonical_url=canonical_url,
        content_fingerprint=content_fingerprint,
    )


def run(article=None, **candidates):
    return decide(
        article if article is not None else normalized(),
        source_id=SOURCE,
        by_external_id=candidates.get("by_external_id"),
        by_canonical_url=candidates.get("by_canonical_url"),
        by_fingerprint=candidates.get("by_fingerprint"),
        **{k: v for k, v in candidates.items() if k == "min_fingerprint_chars"},
    )


def test_no_candidates_creates():
    assert run().kind is DecisionKind.CREATE


def test_external_id_with_unchanged_url_and_content_is_identity_duplicate():
    existing = match()
    decision = run(by_external_id=existing)
    assert decision.kind is DecisionKind.IDENTITY_DUPLICATE
    assert decision.article == existing


def test_external_id_with_changed_content_updates():
    decision = run(by_external_id=match(content_fingerprint=OTHER_FINGERPRINT))
    assert decision.kind is DecisionKind.UPDATE
    assert decision.article.id == 10


def test_external_id_with_new_url_and_unchanged_content_updates():
    """A provider revision that only moves the link is still an update."""

    decision = run(by_external_id=match(canonical_url=OTHER_URL))
    assert decision.kind is DecisionKind.UPDATE


def test_external_id_with_new_url_and_changed_content_updates():
    decision = run(
        by_external_id=match(canonical_url=OTHER_URL, content_fingerprint=OTHER_FINGERPRINT)
    )
    assert decision.kind is DecisionKind.UPDATE


def test_external_id_match_precedes_url_match():
    """The same Article reached by both keys is evaluated as one identity."""

    existing = match()
    decision = run(by_external_id=existing, by_canonical_url=existing)
    assert decision.kind is DecisionKind.IDENTITY_DUPLICATE
    assert decision.article == existing


def test_external_id_and_other_url_article_of_same_source_is_identity_conflict():
    by_id = match(10, canonical_url=OTHER_URL)
    by_url = match(11)
    decision = run(by_external_id=by_id, by_canonical_url=by_url)
    assert decision.kind is DecisionKind.IDENTITY_CONFLICT
    assert (decision.article, decision.conflicting) == (by_id, by_url)


def test_external_id_and_other_url_article_of_other_source_is_source_identity_conflict():
    by_url = match(11, source_id=OTHER_SOURCE)
    decision = run(by_external_id=match(10, canonical_url=OTHER_URL), by_canonical_url=by_url)
    assert decision.kind is DecisionKind.SOURCE_IDENTITY_CONFLICT
    assert decision.article == by_url
    assert decision.conflicting is None


def test_canonical_url_of_same_source_with_same_content_is_identity_duplicate():
    existing = match()
    decision = run(article=normalized(external_id=""), by_canonical_url=existing)
    assert decision.kind is DecisionKind.IDENTITY_DUPLICATE
    assert decision.article == existing


def test_canonical_url_of_same_source_with_changed_content_updates():
    decision = run(
        article=normalized(external_id=""),
        by_canonical_url=match(content_fingerprint=OTHER_FINGERPRINT),
    )
    assert decision.kind is DecisionKind.UPDATE


def test_cross_source_canonical_url_is_conflict():
    existing = match(source_id=OTHER_SOURCE)
    decision = run(article=normalized(external_id=""), by_canonical_url=existing)
    assert decision.kind is DecisionKind.SOURCE_IDENTITY_CONFLICT
    assert decision.article == existing


def test_eligible_fingerprint_under_a_different_url_is_content_duplicate():
    earliest = match(canonical_url=OTHER_URL)
    decision = run(by_fingerprint=earliest)
    assert decision.kind is DecisionKind.CONTENT_DUPLICATE
    assert decision.article == earliest


def test_fingerprint_below_threshold_creates():
    decision = run(
        article=normalized(length=DEFAULT_MIN_FINGERPRINT_CHARS - 1),
        by_fingerprint=match(canonical_url=OTHER_URL),
    )
    assert decision.kind is DecisionKind.CREATE


@pytest.mark.parametrize(
    ("length", "expected"),
    [(199, DecisionKind.CREATE), (200, DecisionKind.CONTENT_DUPLICATE)],
)
def test_fingerprint_threshold_boundary(length, expected):
    decision = run(article=normalized(length=length), by_fingerprint=match(canonical_url=OTHER_URL))
    assert decision.kind is expected


def test_threshold_is_an_injected_primitive():
    """The application layer supplies the configured value; no settings import."""

    candidate = match(canonical_url=OTHER_URL)
    permissive = decide(
        normalized(length=10),
        source_id=SOURCE,
        by_external_id=None,
        by_canonical_url=None,
        by_fingerprint=candidate,
        min_fingerprint_chars=10,
    )
    assert permissive.kind is DecisionKind.CONTENT_DUPLICATE


def test_fingerprint_on_the_same_url_is_not_a_content_duplicate():
    assert run(by_fingerprint=match()).kind is DecisionKind.CREATE


def test_fingerprint_never_precedes_publication_identity():
    """Content equality can neither override an identity nor a conflict."""

    eligible = match(12, canonical_url=OTHER_URL)
    assert (
        run(by_external_id=match(), by_fingerprint=eligible).kind is DecisionKind.IDENTITY_DUPLICATE
    )
    conflict = run(
        by_external_id=match(10, canonical_url=OTHER_URL),
        by_canonical_url=match(11),
        by_fingerprint=eligible,
    )
    assert conflict.kind is DecisionKind.IDENTITY_CONFLICT
    cross_source = run(
        article=normalized(external_id=""),
        by_canonical_url=match(11, source_id=OTHER_SOURCE),
        by_fingerprint=eligible,
    )
    assert cross_source.kind is DecisionKind.SOURCE_IDENTITY_CONFLICT


def test_same_story_without_identity_or_content_equality_creates():
    """Different publications about one event are never duplicates (ADR-0003)."""

    decision = run(
        article=normalized(
            canonical_url="https://other.example/event/votes",
            fingerprint=OTHER_FINGERPRINT,
            external_id="event-votes",
            length=400,
        ),
        by_fingerprint=None,
    )
    assert decision.kind is DecisionKind.CREATE


def test_decisions_and_matches_are_immutable():
    decision = run(by_external_id=match())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.kind = DecisionKind.CREATE
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.article.id = 99


def test_dedup_domain_imports_no_framework():
    """The decision stays database-free and settings-free by construction."""

    module = ast.parse(pathlib.Path(dedup.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported == {"dataclasses", "enum", ".normalization"}
