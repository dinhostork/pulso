"""Offline extractive Story synthesis (#31): select and order, never write prose.

Every element is a sentence or title copied verbatim from the member
Articles, so the default path cannot state anything no source stated.

    TITLE    the earliest member title (publication order) that is not contested
    SUMMARY  each member's first complete, uncontested body sentence, in
             publication order, de-duplicated, at most MAX_SUMMARY
    CONTEXT  the remaining complete, uncontested sentences, in publication and
             sentence order, de-duplicated, at most MAX_CONTEXT

An element cites every member whose text contains exactly that sentence, in
input order, so a syndicated copy supports the same element as its original.

Disagreement is handled by omission. A sentence (or title) carrying figures is
contested when a sentence of another member Article is about the same point —
at least two shared content words, overlapping by half or more of the shorter
sentence's content words — but carries different figures. Contested sentences
are never emitted, so one side of a disputed count, figure or outcome is never
presented as settled. Detection covers figures written in digits; a dispute
expressed only in number words is not detected.
"""

import re
from collections.abc import Sequence

from news.application.story_ports import (
    ExtractorModel,
    SynthesisArticle,
    SynthesisElement,
    SynthesisError,
    SynthesisErrorKind,
    SynthesisInput,
    SynthesisResult,
)

MAX_SUMMARY = 3
MAX_CONTEXT = 4
MAX_SENTENCE_CHARS = 400
MIN_SENTENCE_WORDS = 4

# A sentence ends at . ! or ? followed by whitespace and a new sentence start,
# so decimals ("4.1") and times ("09:40") stay inside their sentence.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"“‘'A-Z0-9])")
_FIGURE = re.compile(r"\d+(?:[.,]\d+)*")
_CONTENT_WORD = re.compile(r"[a-z]{3,}")
_STOPWORDS = frozenset(
    """
    the and for are was were has have had that this with from said says will
    would after before into over than then they their them its his her our
    not but all any can one who which when where while about also been being
    """.split()
)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    """Complete sentences only: a trailing fragment cut by the bound is dropped."""

    return [
        sentence
        for sentence in _BOUNDARY.split(_clean(text))
        if sentence.rstrip("\"”’'").endswith((".", "!", "?"))
        and len(sentence.split()) >= MIN_SENTENCE_WORDS
        and len(sentence) <= MAX_SENTENCE_CHARS
    ]


def _figures(text: str) -> frozenset[str]:
    return frozenset(figure.replace(",", "") for figure in _FIGURE.findall(text))


def _content(text: str) -> frozenset[str]:
    return frozenset(word for word in _CONTENT_WORD.findall(text.lower()) if word not in _STOPWORDS)


def _same_point(left: str, right: str) -> bool:
    shared = _content(left) & _content(right)
    smaller = min(len(_content(left)), len(_content(right))) or 1
    return len(shared) >= 2 and len(shared) / smaller >= 0.5


class _Claims:
    """Every figure-bearing title and sentence, by the Article that carries it."""

    def __init__(self, articles: Sequence[SynthesisArticle]):
        self.by_article = {
            article.article_id: [
                text
                for text in [_clean(article.title), *_sentences(article.text)]
                if _figures(text)
            ]
            for article in articles
        }

    def contested(self, text: str, article_id: int) -> bool:
        figures = _figures(text)
        if not figures:
            return False
        return any(
            _figures(other) != figures and _same_point(text, other)
            for other_id, claims in self.by_article.items()
            if other_id != article_id
            for other in claims
        )


def _supporters(text: str, articles: Sequence[SynthesisArticle], *, title: bool) -> tuple:
    return tuple(
        article.article_id
        for article in articles
        if (_clean(article.title) == text if title else text in _clean(article.text))
    )


class ExtractiveSynthesizer:
    """The default `StorySynthesizer`: deterministic, offline, extractive."""

    identity = ExtractorModel(provider="extractive", model="lead-sentences", revision="1")

    def synthesize(self, story: SynthesisInput) -> SynthesisResult:
        articles = story.articles
        claims = _Claims(articles)
        title = next(
            (
                _clean(article.title)
                for article in articles
                if _clean(article.title) and not claims.contested(article.title, article.article_id)
            ),
            None,
        )
        if title is None:
            raise SynthesisError(
                SynthesisErrorKind.SYNTHESIZER_FAILED,
                "No member title can be used without stating a disputed figure.",
                model_key=self.identity.model_key,
            )
        elements = [SynthesisElement("TITLE", title, _supporters(title, articles, title=True))]

        usable = {
            article.article_id: [
                sentence
                for sentence in _sentences(article.text)
                if not claims.contested(sentence, article.article_id)
            ]
            for article in articles
        }
        seen: set[str] = set()
        summary = []
        for article in articles:
            lead = next(iter(usable[article.article_id]), None)
            if lead is not None and lead not in seen and len(summary) < MAX_SUMMARY:
                seen.add(lead)
                summary.append(lead)
        context = []
        for article in articles:
            for sentence in usable[article.article_id]:
                if sentence not in seen and len(context) < MAX_CONTEXT:
                    seen.add(sentence)
                    context.append(sentence)
        for kind, sentences in (("SUMMARY", summary), ("CONTEXT", context)):
            elements.extend(
                SynthesisElement(kind, sentence, _supporters(sentence, articles, title=False))
                for sentence in sentences
            )
        return SynthesisResult(elements=tuple(elements))
