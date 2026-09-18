"""Pure Topic/Entity normalization and the rule-based extractor (#30); no database."""

import ast
from pathlib import Path

import pytest

from news.adapters import rule_based_enrichment
from news.adapters.rule_based_enrichment import RuleBasedEnrichmentExtractor
from news.application.story_ports import (
    ArticleText,
    EntityExtractor,
    ExtractedEntity,
    ExtractorModel,
    StoryText,
    TopicExtractor,
)
from news.domain import enrichment
from news.domain.enrichment import normalize_name, slugify


@pytest.mark.parametrize(
    "variant", ["Central Bank", "central bank", "Central  Bank", "CENTRAL\tBANK"]
)
def test_case_and_whitespace_variants_share_one_key(variant):
    assert normalize_name(variant) == "central bank"
    assert slugify(variant) == "central-bank"


def test_punctuation_collapses_and_slugs_are_ascii():
    assert normalize_name("  Almen Hydro, Ltd.  ") == "almen hydro ltd"
    assert normalize_name("St. John's — Harbor") == "st john s harbor"
    assert slugify("São Paulo: Harbor & Port") == "sao-paulo-harbor-port"
    assert slugify("...") == ""


def test_normalization_is_not_entity_resolution():
    assert normalize_name("IBM") != normalize_name("International Business Machines")


def test_normalization_module_is_pure():
    tree = ast.parse(Path(enrichment.__file__).read_text())
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported == {"re", "unicodedata"}


def story(*texts):
    return StoryText(
        story_id=1,
        articles=tuple(
            ArticleText(article_id=index, text=text) for index, text in enumerate(texts, 1)
        ),
    )


EXTRACTOR = RuleBasedEnrichmentExtractor(max_topics=5, max_entities=10)


def entities(*texts):
    return {(e.kind, e.name): e.score for e in EXTRACTOR.extract_entities(story(*texts))}


def test_default_extractor_implements_both_protocols_with_a_stable_identity():
    assert isinstance(EXTRACTOR, TopicExtractor) and isinstance(EXTRACTOR, EntityExtractor)
    assert EXTRACTOR.identity == ExtractorModel("rules", "capitalized-phrases-keywords", "1")
    assert EXTRACTOR.identity.model_key == "rules:capitalized-phrases-keywords@1"
    with pytest.raises(ValueError):
        ExtractorModel("rules", "", "1")


def test_entity_kinds_follow_titles_and_keywords():
    found = entities(
        "Mayor Idris Calloway spoke on Monday. The Varrow City Council approved the budget. "
        "Water rose in the Almen Valley while Oren Vasko watched from Elsby."
    )
    assert found == {
        ("PERSON", "Idris Calloway"): 1.0,
        ("ORGANIZATION", "Varrow City Council"): 1.0,
        ("PLACE", "Almen Valley"): 1.0,
        ("PERSON", "Oren Vasko"): 1.0,
        ("OTHER", "Elsby"): 1.0,
    }


def test_sentence_initial_words_and_dates_are_not_entities():
    found = entities("Storms closed the port. Officials met on Tuesday in March.")
    assert found == {}


def test_a_sentence_initial_word_counts_when_it_also_appears_mid_sentence():
    found = entities("Elsby is quiet. Crews returned to Elsby at noon.")
    assert found == {("OTHER", "Elsby"): 1.0}


def test_scores_are_the_share_of_supporting_articles():
    found = EXTRACTOR.extract_entities(
        story(
            "The Central Bank raised rates.",
            "Analysts expect the Central  Bank to pause.",
            "Markets fell sharply.",
        )
    )
    assert found == (ExtractedEntity("ORGANIZATION", "Central Bank", 2 / 3),)


def test_topics_are_supported_content_words_excluding_names_and_stopwords():
    topics = EXTRACTOR.extract_topics(
        story(
            "Storm damage closed the harbor. Engineers inspect the storm damage.",
            "The harbor stays closed after storm damage, Oren Vasko said.",
        )
    )
    labels = [topic.label for topic in topics]
    assert labels[:3] == ["damage", "storm", "closed"]
    assert all(topic.score in (0.5, 1.0) for topic in topics)
    assert not {"said", "after", "oren", "vasko"} & set(labels)
    assert len(topics) <= 5


def test_extraction_is_deterministic_and_bounded():
    texts = [
        f"Company{index} Authority opened. Report number {index} filed." for index in range(30)
    ]
    bounded = RuleBasedEnrichmentExtractor(max_topics=3, max_entities=4)
    first = bounded.extract_entities(story(*texts)), bounded.extract_topics(story(*texts))
    assert first == (bounded.extract_entities(story(*texts)), bounded.extract_topics(story(*texts)))
    assert len(first[0]) == 4 and len(first[1]) == 3


def test_extractor_module_imports_no_django():
    tree = ast.parse(Path(rule_based_enrichment.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith(("django", "news.models")) for module in imported)
