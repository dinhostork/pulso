"""Source-grounded Story synthesis against real PostgreSQL (#31)."""

import ast
import dataclasses
import inspect
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test.utils import CaptureQueriesContext

from news.adapters import extractive_synthesis
from news.application import story_synthesis
from news.application.story_ports import (
    ExtractorModel,
    SynthesisArticle,
    SynthesisElement,
    SynthesisError,
    SynthesisErrorKind,
    SynthesisInput,
    SynthesisResult,
)
from news.application.story_synthesis import ordered_elements, synthesize_story
from news.domain import stories
from news.domain.stories import member_signature
from news.models import (
    Article,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
)
from tests.news.story_corpus import load_corpus

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "stories" / "synthesis"
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
SECRET = "DO_NOT_LEAK_THIS_ARTICLE_TEXT"
HARBOR = [
    (
        "elsby-courier",
        "Storm forces closure of Elsby harbor",
        "The Elsby Harbor Authority closed the harbor on Monday after gales damaged two piers. "
        "Harbor master Oren Vasko said no vessels may leave until engineers inspect the damage.",
    ),
    (
        "kestrel-post",
        "Elsby harbor shut after gales damage piers",
        "Elsby's harbor was closed to all shipping on Monday when storm winds broke two piers. "
        "Inspections are expected to take several days.",
    ),
    (
        "meridian-daily",
        "Fishing fleet stranded as gale wrecks waterfront",
        "Dozens of trawlers sat idle in Elsby on Monday after a violent night of wind. "
        "Port officials barred all traffic while repairs are assessed.",
    ),
]


@pytest.fixture(autouse=True)
def offline_endpoint_dns(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda _host, _port: ("8.8.8.8",))


_counter = iter(range(1, 100_000))


def make_article(source_slug, title, body, *, minutes=0):
    number = next(_counter)
    source, _ = Source.objects.get_or_create(slug=source_slug, defaults={"name": source_slug})
    endpoint = SourceEndpoint.objects.create(
        source=source, kind=SourceEndpoint.Kind.RSS, url=f"https://s{number}.example/feed.xml"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=f"item-{number}",
        external_id=f"item-{number}",
        url=f"https://s{number}.example/item",
        payload={"title": title},
        payload_hash="a" * 64,
        fetched_at=NOW,
    )
    return Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=f"item-{number}",
        canonical_url=f"https://s{number}.example/item",
        title=title,
        body_text=body,
        language="en",
        content_fingerprint=f"{number:064d}",
        published_at=NOW + timedelta(minutes=minutes),
        first_seen_at=NOW,
    )


def make_story(*articles):
    story = Story.objects.create(language="en")
    for article in articles:
        StoryArticle.objects.create(
            story=story, article=article, is_primary=True, method=StoryArticle.Method.MANUAL
        )
    return story


def harbor_story():
    articles = [make_article(*values, minutes=index * 30) for index, values in enumerate(HARBOR)]
    return make_story(*articles), articles


def sequence(synthesis_id):
    return [
        (element.kind, element.position, element.text) for element in ordered_elements(synthesis_id)
    ]


def provenance():
    return (
        list(Article.objects.order_by("pk").values()),
        list(RawArticle.objects.order_by("pk").values()),
    )


class StubSynthesizer:
    """Deterministic double: returns a fixed result (or raises) and counts calls."""

    identity = ExtractorModel("stub", "fixed", "1")

    def __init__(self, build=None, error=None):
        self.build, self.error, self.calls = build, error, 0

    def synthesize(self, story):
        self.calls += 1
        if self.error:
            raise self.error
        return self.build(story)


def first_title(story):
    article = story.articles[0]
    return SynthesisElement("TITLE", article.title, (article.article_id,))


# --- persistence and provenance ------------------------------------------------


@pytest.mark.django_db
def test_story_without_members_raises_and_stores_nothing():
    story = make_story()
    with pytest.raises(SynthesisError) as error:
        synthesize_story(story.pk)
    assert error.value.kind == SynthesisErrorKind.NO_MEMBERS
    assert not StorySynthesis.objects.exists()


@pytest.mark.django_db
def test_one_query_answers_which_articles_support_an_element():
    story, articles = harbor_story()
    summary = synthesize_story(story.pk)
    ids = [article.pk for article in articles]
    elements = list(ordered_elements(summary.synthesis_id))
    chosen = [
        next(element for element in elements if element.kind == kind)
        for kind in ("SUMMARY", "CONTEXT")
    ]

    for element in chosen:
        with CaptureQueriesContext(connections["default"]) as queries:
            supporters = list(
                StorySynthesisElementSource.objects.filter(element=element)
                .order_by("position")
                .values_list("article_id", flat=True)
            )
        assert len(queries.captured_queries) == 1
        assert supporters and set(supporters) <= set(ids)
        for article_id in supporters:
            member = Article.objects.get(pk=article_id)
            assert element.text in " ".join(member.body_text.split())
    assert len({article.source_id for article in articles}) == 3


@pytest.mark.django_db
def test_one_join_answers_which_articles_were_inputs():
    story, articles = harbor_story()
    given = []

    class Recording(extractive_synthesis.ExtractiveSynthesizer):
        def synthesize(self, story_input):
            given.extend(article.article_id for article in story_input.articles)
            return super().synthesize(story_input)

    summary = synthesize_story(story.pk, synthesizer=Recording())

    with CaptureQueriesContext(connections["default"]) as queries:
        inputs = set(
            Article.objects.filter(synthesis_sources__element__synthesis_id=summary.synthesis_id)
            .distinct()
            .values_list("pk", flat=True)
        )
    assert len(queries.captured_queries) == 1
    assert inputs == set(given) == {article.pk for article in articles}
    assert summary.input_article_count == 3


@pytest.mark.django_db
def test_every_element_has_a_source_and_exactly_one_title():
    story, _ = harbor_story()
    summary = synthesize_story(story.pk)

    elements = StorySynthesisElement.objects.filter(synthesis_id=summary.synthesis_id)
    assert not elements.filter(sources__isnull=True).exists()
    assert elements.filter(kind="TITLE").count() == 1
    assert summary.element_count == elements.count() >= 3
    first = sequence(summary.synthesis_id)[0]
    assert first == ("TITLE", 0, HARBOR[0][1])


@pytest.mark.django_db
def test_constraints_reject_a_second_title_duplicates_and_a_second_current():
    story, articles = harbor_story()
    synthesis = StorySynthesis.objects.get(pk=synthesize_story(story.pk).synthesis_id)
    title = synthesis.elements.get(kind="TITLE")
    source = title.sources.first()

    for create in (
        lambda: StorySynthesisElement.objects.create(
            synthesis=synthesis, kind="TITLE", position=1, text="Another title"
        ),
        lambda: StorySynthesisElement.objects.create(
            synthesis=synthesis, kind="SUMMARY", position=0, text="Duplicate position"
        ),
        lambda: StorySynthesisElementSource.objects.create(
            element=title, article_id=source.article_id, position=9
        ),
        lambda: StorySynthesis.objects.create(
            story=story, model_key="stub:fixed@1", member_signature="a" * 64
        ),
        lambda: StorySynthesis.objects.create(
            story=story, model_key="", member_signature="a" * 64, is_current=False
        ),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            create()


@pytest.mark.django_db
def test_rows_carry_model_key_and_generated_at():
    story, _ = harbor_story()
    synthesize_story(story.pk)
    for synthesis in StorySynthesis.objects.all():
        assert synthesis.model_key == "extractive:lead-sentences@1"
        assert synthesis.generated_at is not None
        assert len(synthesis.member_signature) == 64


# --- ordering, idempotency and replacement ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_element_order_is_stable_across_connections_and_rebuilds():
    story, _ = harbor_story()
    summary = synthesize_story(story.pk)
    expected = sequence(summary.synthesis_id)
    reads = []

    def read():
        close_old_connections()
        try:
            reads.append(sequence(summary.synthesis_id))
        finally:
            connections.close_all()

    for _ in range(2):
        thread = threading.Thread(target=read)
        thread.start()
        thread.join(timeout=30)
    StorySynthesis.objects.all().delete()
    rebuilt = synthesize_story(story.pk)

    assert reads == [expected, expected]
    assert sequence(rebuilt.synthesis_id) == expected
    assert [kind for kind, _position, _text in expected][0] == "TITLE"


@pytest.mark.django_db
def test_same_membership_and_model_reuse_the_current_synthesis():
    story, _ = harbor_story()
    stub = StubSynthesizer(lambda s: SynthesisResult((first_title(s),)))

    first = synthesize_story(story.pk, synthesizer=stub)
    again = synthesize_story(story.pk, synthesizer=stub)

    assert first.created and not again.created
    assert again.synthesis_id == first.synthesis_id
    assert stub.calls == 1
    assert StorySynthesis.objects.filter(story=story).count() == 1


@pytest.mark.django_db
def test_membership_change_promotes_a_new_generation_and_keeps_provenance():
    story, articles = harbor_story()
    first = synthesize_story(story.pk)
    extra = make_article(
        "lowland-review",
        "Harbor repairs to take a month",
        "Engineers said the damaged piers need a month of repairs.",
        minutes=200,
    )
    before = provenance()

    StoryArticle.objects.create(
        story=story, article=extra, is_primary=True, method=StoryArticle.Method.MANUAL
    )
    second = synthesize_story(story.pk)

    assert second.created and second.synthesis_id != first.synthesis_id
    assert second.member_signature != first.member_signature
    current = StorySynthesis.objects.get(story=story, is_current=True)
    assert current.pk == second.synthesis_id
    assert StorySynthesis.objects.get(pk=first.synthesis_id).is_current is False
    assert provenance() == before


@pytest.mark.django_db
def test_revising_a_member_changes_the_signature():
    story, articles = harbor_story()
    first = synthesize_story(story.pk)

    Article.objects.filter(pk=articles[0].pk).update(updated_at=NOW + timedelta(days=1))

    assert synthesize_story(story.pk).member_signature != first.member_signature


# --- failures ---------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("build", "error", "kind"),
    [
        (None, RuntimeError(f"provider echoed {SECRET}"), "SYNTHESIZER_FAILED"),
        (lambda s: SynthesisResult(()), None, "INVALID_OUTPUT"),
        (lambda s: SynthesisResult((first_title(s), first_title(s))), None, "INVALID_OUTPUT"),
        (
            lambda s: SynthesisResult((SynthesisElement("SUMMARY", "Only a summary.", (1,)),)),
            None,
            "INVALID_OUTPUT",
        ),
        (
            lambda s: SynthesisResult((SynthesisElement("TITLE", "Foreign", (987654,)),)),
            None,
            "INVALID_OUTPUT",
        ),
        (
            lambda s: SynthesisResult((first_title(s), SynthesisElement("CONTEXT", "Orphan.", ()))),
            None,
            "INVALID_OUTPUT",
        ),
    ],
)
def test_failed_or_invalid_synthesis_keeps_the_current_generation(build, error, kind, pulso_caplog):
    story, articles = harbor_story()
    current = synthesize_story(story.pk)
    StoryArticle.objects.create(
        story=story,
        article=make_article("lowland-review", "Later", f"Later report {SECRET}.", minutes=300),
        is_primary=True,
        method=StoryArticle.Method.MANUAL,
    )
    before = sequence(current.synthesis_id)

    with pytest.raises(SynthesisError) as raised:
        synthesize_story(story.pk, synthesizer=StubSynthesizer(build, error))

    assert raised.value.kind == kind
    assert SECRET not in str(raised.value)
    assert StorySynthesis.objects.get(story=story, is_current=True).pk == current.synthesis_id
    assert StorySynthesis.objects.filter(story=story).count() == 1
    assert sequence(current.synthesis_id) == before
    (record,) = [r for r in pulso_caplog.records if r.message == "News Story synthesis failed"]
    assert (record.story_id, record.error_kind) == (story.pk, kind)
    assert SECRET not in pulso_caplog.text


# --- extractive rules -------------------------------------------------------------


@pytest.mark.django_db
def test_extractive_output_is_verbatim_member_text_across_the_corpus():
    loaded = load_corpus()
    by_event = {}
    for record in loaded.corpus.articles:
        by_event.setdefault(record.expected_event, []).append(loaded.article_ids[record.id])
    for article_ids in by_event.values():
        story = make_story(*Article.objects.filter(pk__in=article_ids))
        synthesis = synthesize_story(story.pk)
        for element in ordered_elements(synthesis.synthesis_id):
            supporters = Article.objects.filter(synthesis_sources__element=element)
            assert supporters.exists()
            for member in supporters:
                source_text = member.title if element.kind == "TITLE" else member.body_text
                assert element.text in " ".join(source_text.split()), element.text


@pytest.mark.django_db
def test_syndicated_copies_support_the_same_elements():
    loaded = load_corpus()
    ids = [loaded.article_ids[fixture] for fixture in ("rail-strike-01", "rail-strike-02")]
    story = make_story(*Article.objects.filter(pk__in=ids))

    synthesis = synthesize_story(story.pk)

    for element in ordered_elements(synthesis.synthesis_id):
        assert sorted(element.sources.values_list("article_id", flat=True)) == sorted(ids)


@pytest.mark.django_db
def test_conflicting_figures_are_omitted_never_stated_as_settled():
    fixture = json.loads((FIXTURES / "conflicting_claims.json").read_text())
    articles = {
        record["id"]: make_article(
            record["source_slug"],
            record["title"],
            record["body"],
            minutes=record["published_offset_minutes"],
        )
        for record in fixture["articles"]
    }
    expected = fixture["expected"]
    assert expected["rule"] == "omit"

    synthesis = synthesize_story(make_story(*articles.values()).pk)

    elements = list(ordered_elements(synthesis.synthesis_id))
    for element in elements:
        for figure in expected["disputed_figures"]:
            assert figure not in element.text, element.text

    def shape(kind):
        return [
            {
                "text": element.text,
                "article_ids": list(
                    element.sources.order_by("position").values_list("article_id", flat=True)
                ),
            }
            for element in elements
            if element.kind == kind
        ]

    def expected_shape(entries):
        return [
            {"text": entry["text"], "article_ids": [articles[i].pk for i in entry["article_ids"]]}
            for entry in entries
        ]

    title = elements[0]
    assert (title.kind, title.text) == ("TITLE", expected["title"])
    assert list(title.sources.values_list("article_id", flat=True)) == [
        articles[i].pk for i in expected["title_article_ids"]
    ]
    assert shape("SUMMARY") == expected_shape(expected["summary"])
    assert shape("CONTEXT") == expected_shape(expected["context"])


def test_agreeing_figures_are_kept():
    def article(article_id, text):
        return SynthesisArticle(article_id, "s", f"Title {article_id}", NOW, text)

    result = extractive_synthesis.ExtractiveSynthesizer().synthesize(
        SynthesisInput(
            1,
            (
                article(1, "Officials said 12 people were injured in the fire."),
                article(2, "Officials said 12 people were injured in the fire on Tuesday."),
            ),
        )
    )
    texts = [element.text for element in result.elements]
    assert "Officials said 12 people were injured in the fire." in texts


def test_decimals_do_not_split_sentences():
    story = SynthesisInput(
        1,
        (
            SynthesisArticle(
                1,
                "s",
                "Quake",
                NOW,
                "A magnitude 4.1 earthquake shook the bay at 09:40 on Monday. No damage was seen.",
            ),
        ),
    )
    texts = [
        e.text for e in extractive_synthesis.ExtractiveSynthesizer().synthesize(story).elements
    ]
    assert "A magnitude 4.1 earthquake shook the bay at 09:40 on Monday." in texts


# --- member_signature and structural guards -------------------------------------------


def test_member_signature_is_order_independent_and_revision_sensitive():
    members = [(3, NOW), (1, NOW + timedelta(hours=1)), (2, NOW)]
    signature = member_signature(members)
    assert signature == member_signature(list(reversed(members)))
    assert len(signature) == 64 and int(signature, 16) >= 0
    assert member_signature(members[:2]) != signature
    assert member_signature([(3, NOW + timedelta(seconds=1)), *members[1:]]) != signature
    same_instant = [(3, NOW.astimezone()), (1, NOW + timedelta(hours=1)), (2, NOW)]
    assert member_signature(same_instant) == signature


def test_member_signature_is_stable_across_processes():
    script = (
        "from datetime import datetime, UTC;"
        "from news.domain.stories import member_signature as m;"
        "print(m([(2, datetime(2026, 3, 2, tzinfo=UTC)), (1, datetime(2026, 3, 1, tzinfo=UTC))]))"
    )
    here = member_signature(
        [(2, datetime(2026, 3, 2, tzinfo=UTC)), (1, datetime(2026, 3, 1, tzinfo=UTC))]
    )
    for seed in ("0", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": "."},
        )
        assert result.stdout.strip() == here


FORBIDDEN = ("user", "account", "session", "interest", "rank", "opinion", "position", "perspective")


def test_synthesis_boundary_is_impersonal():
    for value in (SynthesisInput, SynthesisArticle, SynthesisResult, SynthesisElement):
        for field in dataclasses.fields(value):
            assert not any(word in field.name.lower() for word in FORBIDDEN), (value, field)
    assert list(inspect.signature(synthesize_story).parameters) == ["story_id", "synthesizer"]
    for module in (story_synthesis, extractive_synthesis, stories):
        tree = ast.parse(Path(module.__file__).read_text())
        imported = {
            alias.name if isinstance(node, ast.Import) else (node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert not any(
            part in name for name in imported for part in ("recommend", "opinion", "account")
        ), (module, imported)


def test_synthesis_rows_are_derived_output_not_publications():
    for model in (StorySynthesis, StorySynthesisElement, StorySynthesisElementSource):
        related = {
            field.related_model for field in model._meta.concrete_fields if field.is_relation
        }
        assert Source not in related and SourceEndpoint not in related and RawArticle not in related
    assert {field.name for field in StorySynthesis._meta.concrete_fields} == {
        "id",
        "story",
        "model_key",
        "generated_at",
        "is_current",
        "member_signature",
    }
