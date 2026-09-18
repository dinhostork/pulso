"""Source-grounded Story synthesis (#31).

`synthesize_story(story_id)` reads the Story's current membership, asks a
`StorySynthesizer` for an ordered sequence of elements with their supporting
Articles, validates it and stores it as one new current generation. It takes
no user, and nothing user-derived reaches it: every reader sees the same
synthesis for the same Story state (ADR-0008).

Workflow
    snapshot  member_signature over every current member (`Article.updated_at`
              is the revision marker) and a bounded input: at most
              NEWS_STORY_SYNTHESIS_MAX_ARTICLES members in publication order,
              each with its title and at most
              NEWS_STORY_SYNTHESIS_MAX_CHARS_PER_ARTICLE characters of body
              (or description) text.
    reuse     a current generation with the same member_signature and
              model_key is returned as is; the synthesizer is not called.
    compute   the synthesizer runs with no transaction open, and its result is
              validated before anything is written: exactly one TITLE,
              non-empty texts, at least one supporting Article per element,
              and every supporting Article a member of the snapshot.
    promote   one transaction, holding the Story row lock, demotes the previous
              current generation and writes the complete new one. A reader sees
              the old generation or the new one, never a half-written one.

A failure raises `SynthesisError` and logs identifiers only; the previous
current synthesis stays in place. When refresh runs is decided by the Story
refresh lifecycle (#32).
"""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.db.models.functions import Coalesce

from news.adapters.extractive_synthesis import ExtractiveSynthesizer
from news.application.story_ports import (
    ELEMENT_KINDS,
    StorySynthesizer,
    SynthesisArticle,
    SynthesisElement,
    SynthesisError,
    SynthesisErrorKind,
    SynthesisInput,
    SynthesisResult,
)
from news.domain.stories import member_signature
from news.logging import ingestion_logger
from news.models import (
    Article,
    Story,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
)

SYNTHESIS_LOGGER = "pulso.news.stories"
MAX_ELEMENTS = 20
MAX_ELEMENT_CHARS = 1000
KIND_ORDER = Case(
    *(When(kind=kind, then=Value(index)) for index, kind in enumerate(ELEMENT_KINDS)),
    output_field=IntegerField(),
)


@dataclass(frozen=True)
class SynthesisSummary:
    """Log-safe result: identifiers and counts, never text or model instances."""

    story_id: int
    synthesis_id: int
    model_key: str
    member_signature: str
    input_article_count: int
    element_count: int
    created: bool


@dataclass(frozen=True)
class _Snapshot:
    signature: str
    input: SynthesisInput


@dataclass(frozen=True)
class ComputedSynthesis:
    model_key: str
    elements: tuple[SynthesisElement, ...]
    input_article_count: int


def synthesis_input_from_members(story_id: int, members: tuple[object, ...]) -> SynthesisInput:
    """Apply #31's bounds and ordering to the captured refresh members."""

    ordered = sorted(members, key=lambda member: (member.event_time, member.article_id))
    return SynthesisInput(
        story_id=story_id,
        articles=tuple(
            SynthesisArticle(
                article_id=member.article_id,
                source_slug=member.source_slug,
                title=" ".join(member.title.split()),
                published_at=member.event_time,
                text=" ".join((member.body_text or member.description).split())[
                    : settings.NEWS_STORY_SYNTHESIS_MAX_CHARS_PER_ARTICLE
                ],
            )
            for member in ordered[: settings.NEWS_STORY_SYNTHESIS_MAX_ARTICLES]
        ),
    )


def _snapshot(story_id: int) -> _Snapshot:
    members = Article.objects.filter(story_articles__story_id=story_id)
    signature = member_signature(members.values_list("pk", "updated_at"))
    rows = (
        members.annotate(event_time=Coalesce("published_at", "first_seen_at"))
        .order_by("event_time", "pk")
        .values_list("pk", "source__slug", "title", "event_time", "description", "body_text")[
            : settings.NEWS_STORY_SYNTHESIS_MAX_ARTICLES
        ]
    )
    articles = tuple(
        SynthesisArticle(
            article_id=article_id,
            source_slug=source_slug,
            title=" ".join(title.split()),
            published_at=event_time,
            text=" ".join((body_text or description).split())[
                : settings.NEWS_STORY_SYNTHESIS_MAX_CHARS_PER_ARTICLE
            ],
        )
        for article_id, source_slug, title, event_time, description, body_text in rows
    )
    return _Snapshot(signature, SynthesisInput(story_id=story_id, articles=articles))


def ordered_elements(synthesis_id: int):
    """A generation's elements in reading order: (TITLE, SUMMARY, CONTEXT; position)."""

    return (
        StorySynthesisElement.objects.filter(synthesis_id=synthesis_id)
        .annotate(kind_order=KIND_ORDER)
        .order_by("kind_order", "position")
    )


def _invalid(message: str, model_key: str) -> SynthesisError:
    return SynthesisError(SynthesisErrorKind.INVALID_OUTPUT, message, model_key=model_key)


def _validated(result: object, members: set[int], model_key: str) -> tuple[SynthesisElement, ...]:
    if not isinstance(result, SynthesisResult):
        raise _invalid("Synthesizer returned no SynthesisResult.", model_key)
    elements = tuple(result.elements)
    if not 1 <= len(elements) <= MAX_ELEMENTS:
        raise _invalid("Synthesizer returned no elements or too many.", model_key)
    titles = sum(1 for element in elements if getattr(element, "kind", None) == "TITLE")
    if titles != 1:
        raise _invalid(f"Synthesis must have exactly one TITLE, got {titles}.", model_key)
    for element in elements:
        if (
            not isinstance(element, SynthesisElement)
            or element.kind not in ELEMENT_KINDS
            or not isinstance(element.text, str)
            or not element.text.strip()
            or len(element.text) > MAX_ELEMENT_CHARS
        ):
            raise _invalid("Synthesizer returned a malformed element.", model_key)
        supporters = tuple(element.article_ids)
        if not supporters or len(set(supporters)) != len(supporters):
            raise _invalid("Every element needs distinct supporting Articles.", model_key)
        if not set(supporters) <= members:
            raise _invalid("An element cites an Article outside the Story.", model_key)
    return elements


def _fail(story_id: int, error: SynthesisError) -> SynthesisError:
    ingestion_logger(SYNTHESIS_LOGGER).warning(
        "News Story synthesis failed",
        extra={"story_id": story_id, "error_kind": str(error.kind), "model_key": error.model_key},
    )
    return error


def _summary(synthesis: StorySynthesis, snapshot: _Snapshot, created: bool) -> SynthesisSummary:
    return SynthesisSummary(
        story_id=synthesis.story_id,
        synthesis_id=synthesis.pk,
        model_key=synthesis.model_key,
        member_signature=synthesis.member_signature,
        input_article_count=len(snapshot.input.articles),
        element_count=synthesis.elements.count(),
        created=created,
    )


def _current(story_id: int, signature: str, model_key: str) -> StorySynthesis | None:
    return StorySynthesis.objects.filter(
        story_id=story_id, is_current=True, member_signature=signature, model_key=model_key
    ).first()


def compute_story_synthesis(
    prepared: SynthesisInput, *, synthesizer: StorySynthesizer | None = None
) -> ComputedSynthesis:
    """Run and validate the synthesizer without ORM access or writes."""

    synthesizer = synthesizer or ExtractiveSynthesizer()
    model_key = synthesizer.identity.model_key
    try:
        if not prepared.articles:
            raise SynthesisError(
                SynthesisErrorKind.NO_MEMBERS, "Story has no member Articles.", model_key=model_key
            )
        try:
            result = synthesizer.synthesize(prepared)
        except SynthesisError:
            raise
        except Exception:
            raise SynthesisError(
                SynthesisErrorKind.SYNTHESIZER_FAILED, "Synthesizer failed.", model_key=model_key
            ) from None
        elements = _validated(
            result, {article.article_id for article in prepared.articles}, model_key
        )
    except SynthesisError as error:
        raise _fail(prepared.story_id, error) from None
    return ComputedSynthesis(model_key, elements, len(prepared.articles))


def persist_story_synthesis(
    story_id: int, signature: str, computed: ComputedSynthesis
) -> StorySynthesis:
    """Promote already validated elements; caller holds the Story lock."""

    StorySynthesis.objects.filter(story_id=story_id, is_current=True).update(is_current=False)
    synthesis = StorySynthesis.objects.create(
        story_id=story_id, model_key=computed.model_key, member_signature=signature
    )
    positions = dict.fromkeys(ELEMENT_KINDS, 0)
    for element in computed.elements:
        row = StorySynthesisElement.objects.create(
            synthesis=synthesis,
            kind=element.kind,
            position=positions[element.kind],
            text=element.text,
        )
        positions[element.kind] += 1
        StorySynthesisElementSource.objects.bulk_create(
            StorySynthesisElementSource(element=row, article_id=article_id, position=index)
            for index, article_id in enumerate(element.article_ids)
        )
    return synthesis


def synthesize_story(
    story_id: int, *, synthesizer: StorySynthesizer | None = None
) -> SynthesisSummary:
    """Store a new current synthesis for the Story, or return the unchanged one."""

    synthesizer = synthesizer or ExtractiveSynthesizer()
    model_key = synthesizer.identity.model_key
    Story.objects.only("pk").get(pk=story_id)
    snapshot = _snapshot(story_id)
    try:
        if not snapshot.input.articles:
            raise _fail(
                story_id,
                SynthesisError(
                    SynthesisErrorKind.NO_MEMBERS,
                    "Story has no member Articles.",
                    model_key=model_key,
                ),
            )
        existing = _current(story_id, snapshot.signature, model_key)
        if existing is not None:
            return _summary(existing, snapshot, created=False)
        computed = compute_story_synthesis(snapshot.input, synthesizer=synthesizer)
    except SynthesisError as error:
        raise error from None

    with transaction.atomic():
        Story.objects.select_for_update().get(pk=story_id)
        existing = _current(story_id, snapshot.signature, model_key)
        if existing is not None:
            return _summary(existing, snapshot, created=False)
        synthesis = persist_story_synthesis(story_id, snapshot.signature, computed)
    summary = _summary(synthesis, snapshot, created=True)
    ingestion_logger(SYNTHESIS_LOGGER).info(
        "News Story synthesis completed",
        extra={
            "story_id": story_id,
            "model_key": model_key,
            "input_article_count": summary.input_article_count,
            "element_count": summary.element_count,
        },
    )
    return summary
