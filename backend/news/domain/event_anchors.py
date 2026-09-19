"""Proper-name anchors of a report, for the secondary Story verifier (#36); pure.

Two reports of one event name the same places, bodies and people; two
lookalike reports of different events (a templated earthquake bulletin for
another valley, the same budget vote in another town) name different ones,
even when an embedding puts them close together. This module extracts those
names without a named-entity model, from capitalization alone:

- a *capitalized term* is a word that is capitalized at every occurrence in
  the report (title and text), so `storm` in "Storm forces ..." does not
  count when the body later says "the storm";
- a *strong anchor* is a capitalized term that also occurs mid-sentence in
  the text (not the title), where capitalization marks a name rather than
  the start of a sentence or a headline style.

English function words and calendar names are excluded: they are capitalized
for grammatical reasons, and time is judged by the matcher's time rule. The
rule is only meaningful for languages that capitalize proper names and not
other nouns, so it is declared for English only (`ANCHOR_LANGUAGES`).

Terms are case-folded with a trailing possessive removed. Only counts ever
leave the matching layer; the terms themselves are publication text.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Primary language subtags the anchor rule is defined for.
ANCHOR_LANGUAGES = frozenset({"en"})

_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-](?=[^\W\d_]))*")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?:;])\s+|\n+")
_POSSESSIVE = re.compile(r"['’]s$")

# Words capitalized for grammar, not because they name something.
_FUNCTION_WORDS = frozenset(
    """
    a an the this that these those some any all each every no not nor and or but
    if then than so as at by for from in into of off on onto out over per to up
    upon via with within without about above after against along among around
    before behind below beneath beside between beyond during except inside near
    since through throughout toward towards under until unlike while
    i me my we us our you your he him his she her it its they them their who whom
    whose which what when where why how there here
    is are was were be been being has have had do does did will would shall
    should can could may might must
    one two three four five six seven eight nine ten eleven twelve first second
    third last next new mr mrs ms dr
    """.split()
)
_CALENDAR = frozenset(
    """
    monday tuesday wednesday thursday friday saturday sunday january february
    march april may june july august september october november december
    """.split()
)
_EXCLUDED = _FUNCTION_WORDS | _CALENDAR


@dataclass(frozen=True)
class EventAnchors:
    """Case-folded terms of one report; never persisted or logged."""

    capitalized: frozenset[str]
    strong: frozenset[str]


def _term(word: str) -> str:
    return _POSSESSIVE.sub("", word).casefold()


def _occurrences(text: str) -> Iterable[tuple[str, bool, bool]]:
    """(term, capitalized, mid_sentence) for every word of `text`."""

    for sentence in _SENTENCE_BREAK.split(text):
        for index, match in enumerate(_WORD.finditer(sentence)):
            word = match.group()
            yield _term(word), word[0].isupper(), index > 0


def event_anchors(*, title: str, text: str) -> EventAnchors:
    """Anchors of one report from its bounded title and text."""

    capitalized: dict[str, bool] = {}
    mid_sentence: set[str] = set()
    for term, is_capitalized, _ in _occurrences(title):
        capitalized[term] = capitalized.get(term, True) and is_capitalized
    for term, is_capitalized, mid in _occurrences(text):
        capitalized[term] = capitalized.get(term, True) and is_capitalized
        if mid and is_capitalized:
            mid_sentence.add(term)
    names = frozenset(
        term
        for term, always in capitalized.items()
        if always and len(term) > 1 and term not in _EXCLUDED
    )
    return EventAnchors(capitalized=names, strong=names & frozenset(mid_sentence))


def shared_anchor_count(article: EventAnchors, members: Iterable[EventAnchors]) -> int:
    """Names the report and a Story's members have in common.

    A term counts when it is a strong anchor on one side and capitalized on the
    other, so a name that only opens a sentence in one report still matches a
    mid-sentence mention in the other.
    """

    members = tuple(members)
    member_capitalized = frozenset().union(*(member.capitalized for member in members))
    member_strong = frozenset().union(*(member.strong for member in members))
    shared = (article.strong & member_capitalized) | (member_strong & article.capitalized)
    return len(shared)
