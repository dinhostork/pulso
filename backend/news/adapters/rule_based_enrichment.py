"""Offline, deterministic Topic and Entity extraction by simple rules (#30).

No model, no network and no dependency: it reads the Story's member text and
reports only strings that literally occur there.

Entities are runs of capitalized words ("Elsby Harbor Authority", allowing
"of"/"and" inside). A leading personal title ("Mayor", "Minister", ...) is
dropped and marks a PERSON; a final organization word ("Council",
"Authority", ...) marks an ORGANIZATION; a final place word ("Bay",
"Valley", ...) marks a PLACE; two or three bare capitalized words are taken
as a PERSON; anything else is OTHER. A single capitalized word that only ever
starts a sentence is ignored, as are weekday and month names.

Topics are content words of four or more letters that are not stopwords and
not part of an entity name, ranked by how many member Articles use them.

Scores are the share of member Articles in which the item occurs. Ties break
on the normalized name, so the output never depends on dictionary order.
"""

import re
from collections import Counter, defaultdict

from news.application.story_ports import (
    ExtractedEntity,
    ExtractedTopic,
    ExtractorModel,
    StoryText,
)
from news.domain.enrichment import normalize_name

_CAPITALIZED = r"[A-Z][\w'’-]*"
_PHRASE = re.compile(rf"{_CAPITALIZED}(?:\s+(?:(?:of|and|de|da|do)\s+)?{_CAPITALIZED})*")
_WORD = re.compile(r"[a-z]{4,}")

TITLES = frozenset(
    "mayor minister president governor senator judge chief curator councillor "
    "dr mr mrs ms sir professor general secretary chancellor prime".split()
)
ORGANIZATION_WORDS = frozenset(
    "authority council bank transit hydro museum office union ministry company "
    "agency university court party hospital club group service services "
    "department commission committee board inc ltd corporation association".split()
)
PLACE_WORDS = frozenset(
    "bay valley river harbor harbour island street lake county province region "
    "city town port mountain coast".split()
)
LEADING_WORDS = frozenset("the a an in on at from after as by for".split())
CALENDAR = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday january february "
    "march april may june july august september october november december".split()
)
STOPWORDS = CALENDAR | frozenset(
    """
    about above after again against also although among another because been
    before being below between both could does doing down during each even
    every from further have having here into itself just last like made make
    many more most much must near next once only other over said says same
    should since some such than that their them then there these they this
    those though three through today told under until upon very were what when
    where which while will with within without would year years your week weeks
    """.split()
)


def _entities_in(text: str) -> list[tuple[str, str]]:
    """(kind, surface name) for every entity mention in one Article."""

    found = []
    for match in _PHRASE.finditer(text):
        words = match.group().split()
        before = text[max(0, match.start() - 8) : match.start()].rstrip(" \t")
        at_sentence_start = match.start() == 0 or not before or before[-1] in ".!?\n"
        while words and words[0].lower() in LEADING_WORDS:
            words.pop(0)
            at_sentence_start = False
        titled = bool(words) and words[0].lower().rstrip(".") in TITLES
        if titled:
            words.pop(0)
        words = [word for word in words if word.lower() not in CALENDAR]
        if not words:
            continue
        last = words[-1].lower()
        if last in ORGANIZATION_WORDS:
            kind = "ORGANIZATION"
        elif last in PLACE_WORDS:
            kind = "PLACE"
        elif titled or 2 <= len(words) <= 3:
            kind = "PERSON"
        else:
            kind = "OTHER"
        if len(words) == 1 and not titled and at_sentence_start:
            kind = "_SENTENCE_START"
        found.append((kind, " ".join(words)))
    return found


def _mentions(story: StoryText) -> dict[tuple[str, str], dict]:
    """Entity mentions keyed by (kind, normalized name), with support counts."""

    per_article = {article.article_id: _entities_in(article.text) for article in story.articles}
    mid_sentence = {
        normalize_name(name)
        for mentions in per_article.values()
        for kind, name in mentions
        if kind != "_SENTENCE_START"
    }
    grouped: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"articles": set(), "surfaces": Counter()}
    )
    for article_id, mentions in per_article.items():
        for kind, name in mentions:
            key = normalize_name(name)
            if kind == "_SENTENCE_START":
                if key not in mid_sentence:
                    continue
                kind = "OTHER"
            entry = grouped[(kind, key)]
            entry["articles"].add(article_id)
            entry["surfaces"][name] += 1
    return grouped


def _display(surfaces: Counter) -> str:
    return sorted(surfaces.items(), key=lambda item: (-item[1], item[0]))[0][0]


class RuleBasedEnrichmentExtractor:
    """Implements both `TopicExtractor` and `EntityExtractor`."""

    identity = ExtractorModel(provider="rules", model="capitalized-phrases-keywords", revision="1")

    def __init__(self, *, max_topics: int, max_entities: int):
        self.max_topics = max_topics
        self.max_entities = max_entities

    def extract_entities(self, story: StoryText) -> tuple[ExtractedEntity, ...]:
        members = len(story.articles) or 1
        grouped = _mentions(story)
        ranked = sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]["articles"]), item[0][1], item[0][0]),
        )
        return tuple(
            ExtractedEntity(
                kind=kind, name=_display(entry["surfaces"]), score=len(entry["articles"]) / members
            )
            for (kind, _key), entry in ranked[: self.max_entities]
        )

    def extract_topics(self, story: StoryText) -> tuple[ExtractedTopic, ...]:
        members = len(story.articles) or 1
        entity_words = {word for (_kind, key) in _mentions(story) for word in key.split()}
        support: Counter = Counter()
        frequency: Counter = Counter()
        for article in story.articles:
            words = [
                word
                for word in _WORD.findall(normalize_name(article.text))
                if word not in STOPWORDS and word not in entity_words
            ]
            frequency.update(words)
            support.update(set(words))
        ranked = sorted(support, key=lambda word: (-support[word], -frequency[word], word))
        return tuple(
            ExtractedTopic(label=word, score=support[word] / members)
            for word in ranked[: self.max_topics]
        )
