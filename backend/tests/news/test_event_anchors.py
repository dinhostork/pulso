"""Proper-name anchors for the secondary Story verifier (#36); pure, no database."""

import ast
from pathlib import Path

import pytest
from django.conf import settings

from news.domain import event_anchors as anchors_module
from news.domain.embeddings import article_embedding_input
from news.domain.event_anchors import EventAnchors, event_anchors, shared_anchor_count
from tests.news.story_corpus import read_corpus


def anchors(title, text):
    return event_anchors(title=title, text=text)


def test_a_name_mid_sentence_is_a_strong_anchor():
    found = anchors("Council meets", "The vote in Varrow passed.")

    assert "varrow" in found.strong
    assert found.strong <= found.capitalized


def test_a_word_only_opening_sentences_is_capitalized_but_not_strong():
    found = anchors("Varrow council approves budget", "Varrow approved the plan. Nothing else.")

    assert "varrow" in found.capitalized and "varrow" not in found.strong


def test_a_word_written_lowercase_anywhere_is_not_a_name():
    found = anchors("Storm forces closure", "The storm hit the Harbor. Workers left the harbor.")

    assert "storm" not in found.capitalized
    assert "harbor" not in found.capitalized


def test_function_words_and_calendar_names_are_never_anchors():
    found = anchors("The vote", "It passed on Monday in May, and The Town agreed. No change.")

    assert {"monday", "may", "the", "no", "and"}.isdisjoint(found.capitalized)
    assert "town" in found.strong


def test_title_case_headlines_do_not_make_strong_anchors():
    found = anchors("Storm Forces Closure Of Elsby Harbor", "Gales hit the town overnight.")

    assert found.strong == frozenset()


def test_possessives_and_case_are_normalized():
    found = anchors("Report", "Residents said Elsby's piers broke near ELSBY docks.")

    assert "elsby" in found.strong


def test_shared_names_count_either_direction():
    opening = anchors("Varrow council approves budget", "Varrow City Council approved it.")
    mid = anchors("Funding in Varrow plan", "Councillors in Varrow backed it.")
    other = anchors("Lowmere budget", "Lowmere Town Council approved it.")

    assert shared_anchor_count(mid, [opening]) == 1
    assert shared_anchor_count(opening, [mid]) == 1
    assert shared_anchor_count(other, [opening, mid]) == 0
    assert shared_anchor_count(mid, []) == 0


def test_module_is_pure_and_holds_no_corpus_knowledge():
    source = Path(anchors_module.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {"re", "collections.abc", "dataclasses"}
    # Code, not prose: every string literal (docstrings excluded) and identifier.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    code = " ".join(
        [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        + [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    ).lower()
    for name in ("varrow", "almen", "kestrel", "elsby", "lowmere", "budget", "earthquake"):
        assert name not in code


# --- the corpus hard cases, from the corpus text itself --------------------------------


@pytest.fixture(scope="module")
def corpus_anchors() -> dict[str, EventAnchors]:
    limit = settings.NEWS_EMBEDDING_MAX_INPUT_CHARS
    return {
        article.id: event_anchors(
            title=article.title,
            text=article_embedding_input(
                title="", description="", body_text=article.body, max_chars=limit
            ),
        )
        for article in read_corpus().articles
    }


@pytest.mark.parametrize(
    ("incoming", "members", "shared"),
    [
        # Same event: the report names what the Story already names.
        ("varrow-budget-02", ["varrow-budget-01"], 1),
        ("varrow-budget-01", ["varrow-budget-02"], 1),
        ("almen-flood-02", ["almen-flood-01"], 1),
        ("almen-flood-03", ["almen-flood-01", "almen-flood-02"], 2),
        ("harbor-storm-03", ["harbor-storm-01", "harbor-storm-02"], 1),
        # Lookalikes of different events name different places.
        ("almen-quake-01", ["kestrel-quake-01"], 0),
        ("lowmere-budget-01", ["varrow-budget-01", "varrow-budget-02"], 0),
        # The inquiry shares the river's name: anchors alone cannot reject it,
        # which is why the verifier also requires a close member.
        ("almen-inquiry-01", ["almen-flood-01", "almen-flood-02", "almen-flood-03"], 1),
    ],
)
def test_corpus_hard_cases(corpus_anchors, incoming, members, shared):
    assert (
        shared_anchor_count(corpus_anchors[incoming], [corpus_anchors[m] for m in members])
        == shared
    )
