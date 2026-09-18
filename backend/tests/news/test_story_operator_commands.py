"""Story operator commands (#33): explanation, inspection, backlog and actions.

Articles come from the #26 corpus and are embedded with the recorded local
model vectors, so every decision below is the real matcher's, with its real
distances (see `recorded_embeddings.py`). Explanations must come from the
evidence recorded at decision time: retrieval is made to fail while a command
runs, so a command that re-ran it could not pass.
"""

import ast
import pathlib
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from news.application import embeddings as embeddings_module
from news.application import story_processing as processing_module
from news.application.story_ports import (
    EmbeddingError,
    EmbeddingErrorKind,
    SynthesisError,
    SynthesisErrorKind,
)
from news.application.story_processing import (
    claim_for_reconciliation,
    embed_step,
    process_article,
)
from news.application.story_refresh import refresh_candidates, refresh_story
from news.application.story_synthesis import ordered_elements
from news.domain.story_matching import MatchReason
from news.management.commands import (
    news_stories,
    news_story,
    news_story_backlog,
    news_story_explain,
)
from news.models import (
    Article,
    Story,
    StoryArticle,
    StorySynthesis,
    StorySynthesisElementSource,
)
from tests.news.recorded_embeddings import RecordedEmbeddingProvider
from tests.news.story_corpus import load_corpus

COMMANDS_DIR = pathlib.Path(news_story.__file__).resolve().parent
STORY_COMMANDS = (
    "news_stories.py",
    "news_story.py",
    "news_story_explain.py",
    "news_story_backlog.py",
    "news_story_process.py",
    "news_story_reconcile.py",
    "news_story_refresh.py",
)


def run(command, *args):
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def run_failing(command, *args):
    """Run a command expected to exit nonzero; return its message and output."""

    out = StringIO()
    with pytest.raises(CommandError) as raised:
        call_command(command, *args, stdout=out, stderr=StringIO())
    return str(raised.value), out.getvalue()


@pytest.fixture
def recorded(monkeypatch):
    """The configured provider replays the pinned local model offline."""

    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: RecordedEmbeddingProvider()
    )


@pytest.fixture
def corpus(db, recorded):
    return load_corpus()


def process(loaded, *fixture_ids):
    """Process in the given order; every affected Story is refreshed before the next."""

    for fixture_id in fixture_ids:
        process_article(loaded.article_ids[fixture_id])
        for story_id in refresh_candidates(limit=1000):
            refresh_story(story_id)


def story_of(loaded, fixture_id):
    return StoryArticle.objects.get(
        article_id=loaded.article_ids[fixture_id], is_primary=True
    ).story_id


def forbid_retrieval(monkeypatch):
    """From here on retrieval fails loudly: an explanation must never run it again."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("explanation re-ran candidate retrieval")

    monkeypatch.setattr("news.application.story_candidates.find_candidates", forbidden)
    monkeypatch.setattr("news.application.story_matching.find_candidates", forbidden)


def line_with(output, prefix):
    return next(line for line in output.splitlines() if line.startswith(prefix))


# --- why was Article X assigned to Story Y ------------------------------------------


@pytest.mark.django_db
def test_explains_a_match_from_the_evidence_recorded_at_decision_time(corpus, monkeypatch):
    process(corpus, "harbor-storm-01", "harbor-storm-02")
    harbor = story_of(corpus, "harbor-storm-01")
    association = StoryArticle.objects.get(article_id=corpus.article_ids["harbor-storm-02"])
    # Later Stories change what retrieval would answer now, not what was recorded.
    process(corpus, "rail-strike-01", "kestrel-quake-01")
    forbid_retrieval(monkeypatch)

    output = run("news_story_explain", "--article", str(corpus.article_ids["harbor-storm-02"]))

    decision = line_with(output, "decision=")
    assert "decision=MATCH reason=WITHIN_THRESHOLD" in decision
    assert f"chosen_story_id={harbor}" in decision
    assert f"distance={association.evidence['distance']:.6f}" in decision
    assert "threshold=0.18" in decision and "candidate_count=1" in decision
    assert f"  #1 story_id={harbor} distance=" in output
    assert "within_threshold=yes" in output
    assert f"story_id={harbor} story_status=ACTIVE method=MATCHED" in output
    assert "processing_state=MATCHED attempts=0" in output and "fresh=yes" in output
    assert f"explanation: joined Story {harbor}" in output


@pytest.mark.django_db
def test_explains_why_no_candidate_qualified_for_a_new_story(corpus, monkeypatch):
    process(corpus, "varrow-budget-01")
    budget = story_of(corpus, "varrow-budget-01")
    process(corpus, "varrow-budget-02")
    forbid_retrieval(monkeypatch)

    output = run("news_story_explain", "--article", str(corpus.article_ids["varrow-budget-02"]))

    decision = line_with(output, "decision=")
    assert "decision=CREATE_NEW_STORY reason=ABOVE_THRESHOLD chosen_story_id=-" in decision
    assert "distance=0.188946" in decision and "threshold=0.18" in decision
    assert "candidate_count=" in decision
    assert f"  #1 story_id={budget} distance=0.188946 within_threshold=no" in output
    assert (
        f"explanation: created a new Story: no candidate qualified; the nearest compatible "
        f"candidate (Story {budget}) was at distance 0.188946, above the threshold 0.18"
    ) in output


@pytest.mark.django_db
def test_explains_a_new_story_when_retrieval_returned_nothing(corpus, monkeypatch):
    process(corpus, "harbor-storm-01")
    forbid_retrieval(monkeypatch)

    output = run("news_story_explain", "--article", str(corpus.article_ids["harbor-storm-01"]))

    assert "decision=CREATE_NEW_STORY reason=NO_CANDIDATES" in output
    assert "candidate_count=0" in output and "recorded_candidates=0" in output
    assert "explanation: created a new Story: retrieval returned no candidate" in output


@pytest.mark.django_db
def test_explains_candidates_that_were_all_incompatible(corpus):
    article_id = corpus.article_ids["chess-final-01"]
    story = Story.objects.create(language="en")
    StoryArticle.objects.create(
        story=story,
        article_id=article_id,
        is_primary=True,
        method=StoryArticle.Method.CREATED_STORY,
        matcher_key="story-match-v1;test",
        evidence={
            "reason": MatchReason.NO_COMPATIBLE_CANDIDATE.value,
            "distance": None,
            "candidate_count": 2,
            "candidates": [{"story_id": 901, "distance": 0.1}, {"story_id": 902, "distance": 0.3}],
            "max_distance": 0.18,
            "max_time_gap_hours": 48.0,
            "embedding_model_key": "fastembed:test",
        },
    )

    output = run("news_story_explain", "--article", str(article_id))

    assert "reason=NO_COMPATIBLE_CANDIDATE" in output
    assert "#1 story_id=901 distance=0.100000" in output
    assert "2 candidate(s) were retrieved but none was compatible" in output
    assert "latest member within 48.0 h" in output


@pytest.mark.django_db
def test_explains_an_article_never_attempted(corpus):
    output = run("news_story_explain", "--article", str(corpus.article_ids["chess-final-01"]))

    assert "processing=MISSING (never attempted)" in output
    assert "association=none" in output
    assert "explanation: never attempted" in output


# --- which Story-derived step failed --------------------------------------------------


class BrokenProvider:
    identity = RecordedEmbeddingProvider.identity

    def embed(self, texts):
        raise EmbeddingError(
            EmbeddingErrorKind.INVALID_OUTPUT, "Safe summary.", model_key=self.identity.model_key
        )


@pytest.mark.django_db
def test_a_failed_embedding_names_the_step_and_kind(corpus, monkeypatch):
    article_id = corpus.article_ids["chess-final-01"]
    monkeypatch.setattr(embeddings_module, "embedding_provider_for", lambda _name: BrokenProvider())
    assert process_article(article_id).state == "FAILED"

    output = run("news_story_explain", "--article", str(article_id))

    assert "processing_state=FAILED attempts=1 error_kind=INVALID_OUTPUT" in output
    assert "failed_step=article_embedding" in output
    assert "retries_exhausted=no" in output


@pytest.mark.django_db
def test_a_failed_match_names_the_matching_step(corpus, monkeypatch):
    article_id = corpus.article_ids["chess-final-01"]

    def archived(*_args, **_kwargs):
        raise processing_module.StoryNoLongerActive

    monkeypatch.setattr(processing_module, "match_article", archived)
    assert process_article(article_id).state == "FAILED"

    output = run("news_story_explain", "--article", str(article_id))

    assert "error_kind=STORY_NO_LONGER_ACTIVE failed_step=story_matching" in output


class BrokenSynthesizer:
    class identity:
        model_key = "stub:broken@1"

    def synthesize(self, prepared):
        raise SynthesisError(
            SynthesisErrorKind.SYNTHESIZER_FAILED, "Safe summary.", model_key="stub:broken@1"
        )


@pytest.mark.django_db
def test_a_failed_story_refresh_names_the_step_and_kind(corpus):
    process(corpus, "harbor-storm-01")
    story_id = story_of(corpus, "harbor-storm-01")
    process_article(corpus.article_ids["harbor-storm-02"])
    assert refresh_story(story_id, synthesizer=BrokenSynthesizer()).outcome == "FAILED"

    detail = run("news_story", "--story", str(story_id))
    message, listing = run_failing("news_stories", "--failed")

    assert "refresh_state=FAILED" in detail
    assert "failed_step=story_synthesis error_kind=SYNTHESIZER_FAILED" in detail
    assert message == "1 FAILED Story refresh(es)"
    row = listing.splitlines()[1].split()
    assert row[0] == str(story_id) and "FAILED" in row
    assert "story_synthesis" in row and "SYNTHESIZER_FAILED" in row


# --- which Articles support Story Y ---------------------------------------------------


@pytest.mark.django_db
def test_story_detail_lists_every_member_and_the_synthesis_provenance(corpus, monkeypatch):
    fixtures = ("harbor-storm-01", "harbor-storm-02", "harbor-storm-03", "harbor-storm-04")
    process(corpus, *fixtures)
    forbid_retrieval(monkeypatch)
    story_id = story_of(corpus, "harbor-storm-01")
    synthesis = StorySynthesis.objects.get(story_id=story_id, is_current=True)

    output = run("news_story", "--story", str(story_id))

    assert f"story_id={story_id} status=ACTIVE language=en" in output
    assert "refresh_state=CURRENT" in output
    assert "article_count=4 source_count=3 live_articles=4 live_sources=3" in output
    assert "members=4 shown=4" in output
    cited = set(
        StorySynthesisElementSource.objects.filter(element__synthesis=synthesis).values_list(
            "article_id", flat=True
        )
    )
    for fixture_id in fixtures:
        article = Article.objects.get(pk=corpus.article_ids[fixture_id])
        member = next(line for line in output.splitlines() if line.split()[:1] == [str(article.pk)])
        assert article.source.slug in member and article.canonical_url in member
        assert ("yes" if article.pk in cited else "no") == member.split()[-2]
        assert f"title: {article.title}" in output
        # Operators see titles and URLs; never the body.
        assert article.body_text not in output
    assert f"synthesis_id={synthesis.pk} model_key={synthesis.model_key}" in output
    assert "matches_story_signature=yes" in output
    for element in ordered_elements(synthesis.pk):
        supporters = ",".join(
            str(pk)
            for pk in element.sources.order_by("position").values_list("article_id", flat=True)
        )
        assert f"  {element.kind}[{element.position}]  supporting_articles={supporters}" in output
        if element.kind != "TITLE":
            # The extractive TITLE is a member's title verbatim, which operators
            # may see; SUMMARY and CONTEXT texts are never printed.
            assert element.text not in output
    assert f"cited_articles={','.join(str(pk) for pk in sorted(cited))}" in output


@pytest.mark.django_db
def test_story_detail_member_listing_is_bounded(corpus):
    process(corpus, "harbor-storm-01", "harbor-storm-02", "harbor-storm-04")
    story_id = story_of(corpus, "harbor-storm-01")

    output = run("news_story", "--story", str(story_id), "--members", "1")

    assert "members=3 shown=1" in output
    assert str(corpus.article_ids["harbor-storm-01"]) in output
    assert f"\n  {corpus.article_ids['harbor-storm-04']} " not in output
    for bound in ("0", "1001"):
        with pytest.raises(CommandError, match="--members must be between 1 and 1000"):
            run("news_story", "--story", str(story_id), "--members", bound)
    with pytest.raises(CommandError, match="No Story with id"):
        run("news_story", "--story", "999999")


# --- recent Stories ------------------------------------------------------------------


@pytest.mark.django_db
def test_recent_stories_are_newest_first_and_bounded(corpus):
    process(corpus, "harbor-storm-01", "rail-strike-01", "kestrel-quake-01")
    ids = [story_of(corpus, f) for f in ("harbor-storm-01", "rail-strike-01", "kestrel-quake-01")]

    output = run("news_stories", "--last", "2")

    lines = output.splitlines()
    assert lines[0].split()[:6] == ["ID", "STATUS", "LANG", "ARTS", "SRCS", "REFRESH"]
    assert [line.split()[0] for line in lines[1:]] == [str(ids[2]), str(ids[1])]
    assert lines[1].split()[1:6] == ["ACTIVE", "en", "1", "1", "CURRENT"]
    assert lines[1].split()[-1] == StorySynthesis.objects.get(story_id=ids[2]).model_key
    for bound in ("0", "-3", "1001"):
        with pytest.raises(CommandError, match="--last must be between 1 and 1000"):
            run("news_stories", "--last", bound)


@pytest.mark.django_db
def test_stale_listing_does_not_fail(corpus):
    process(corpus, "harbor-storm-01")
    process_article(corpus.article_ids["harbor-storm-02"])

    output = run("news_stories", "--stale")

    assert output.splitlines()[1].split()[0] == str(story_of(corpus, "harbor-storm-01"))
    assert run("news_stories", "--failed") == "No Stories.\n"


# --- backlog -------------------------------------------------------------------------


@pytest.fixture
def backlog(corpus, monkeypatch):
    """One Article in each observable incomplete state, plus one complete."""

    ids = corpus.article_ids
    process(corpus, "harbor-storm-01")  # complete: never listed
    claim_for_reconciliation([ids["harbor-storm-02"]])  # PENDING
    embed_step(ids["rail-strike-01"])  # EMBEDDED
    monkeypatch.setattr(embeddings_module, "embedding_provider_for", lambda _name: BrokenProvider())
    process_article(ids["chess-final-01"])  # FAILED at embedding
    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: RecordedEmbeddingProvider()
    )
    return ids


def backlog_rows(output):
    return {
        int(line.split()[0]): line.split()[1]
        for line in output.splitlines()[1:]
        if line.split() and line.split()[0].isdigit()
    }


@pytest.mark.django_db
def test_backlog_distinguishes_missing_pending_embedded_and_failed(backlog):
    message, output = run_failing("news_story_backlog", "--limit", "1000")

    rows = backlog_rows(output)
    assert rows[backlog["harbor-storm-02"]] == "PENDING"
    assert rows[backlog["rail-strike-01"]] == "EMBEDDED"
    assert rows[backlog["chess-final-01"]] == "FAILED"
    assert rows[backlog["museum-maps-01"]] == "MISSING"
    assert backlog["harbor-storm-01"] not in rows
    assert list(rows) == sorted(rows)
    failed_line = next(
        line for line in output.splitlines() if line.startswith(str(backlog["chess-final-01"]))
    )
    assert "article_embedding" in failed_line and "INVALID_OUTPUT" in failed_line
    assert message == "1 FAILED Story processing record(s)"
    assert f"listed={len(rows)} missing={len(rows) - 3} failed=1 limit=1000" in output


@pytest.mark.django_db
def test_backlog_views_and_exit_status(backlog):
    message, failed = run_failing("news_story_backlog", "--failed")
    pending = run("news_story_backlog", "--pending", "--limit", "1000")

    assert backlog_rows(failed) == {backlog["chess-final-01"]: "FAILED"}
    assert "FAILED" not in backlog_rows(pending).values()
    assert backlog_rows(pending)[backlog["harbor-storm-02"]] == "PENDING"


@pytest.mark.django_db
def test_backlog_limit_is_enforced(backlog):
    output = run("news_story_backlog", "--pending", "--limit", "2")

    assert len(backlog_rows(output)) == 2
    for bound in ("0", "1001"):
        with pytest.raises(CommandError, match="--limit must be between 1 and 1000"):
            run("news_story_backlog", "--limit", bound)


@pytest.mark.django_db
def test_backlog_shows_unassigned_and_stale_matches(corpus, monkeypatch):
    process(corpus, "harbor-storm-01", "rail-strike-01")
    StoryArticle.objects.filter(article_id=corpus.article_ids["harbor-storm-01"]).delete()
    monkeypatch.setattr(processing_module, "MATCHER_KEY", "story-match-v2;changed")

    rows = backlog_rows(run("news_story_backlog", "--pending", "--limit", "1000"))

    assert rows[corpus.article_ids["harbor-storm-01"]] == "UNASSIGNED"
    assert rows[corpus.article_ids["rail-strike-01"]] == "STALE"


@pytest.mark.django_db
def test_nothing_incomplete_exits_zero(corpus):
    process(corpus, *corpus.article_ids)

    assert run("news_story_backlog") == "No incomplete Story processing.\n"


# --- actions go through the application services ----------------------------------------


class Recorder:
    def __init__(self, result):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.mark.django_db
def test_inspection_is_read_only_unless_an_action_is_requested(backlog, monkeypatch):
    process_service = Recorder(None)
    refresh_service = Recorder(None)
    reprocess_service = Recorder(None)
    monkeypatch.setattr(news_story_backlog, "process_article", process_service)
    monkeypatch.setattr(news_stories, "refresh_story", refresh_service)
    monkeypatch.setattr(news_story, "refresh_story", refresh_service)
    monkeypatch.setattr(news_story_explain, "reprocess_article", reprocess_service)
    story_id = StoryArticle.objects.get(article_id=backlog["harbor-storm-01"]).story_id

    run("news_story_explain", "--article", str(backlog["harbor-storm-01"]))
    run("news_story", "--story", str(story_id))
    run("news_stories")
    run("news_story_backlog", "--pending")

    assert not (process_service.calls or refresh_service.calls or reprocess_service.calls)


@pytest.mark.django_db
def test_action_flags_call_the_application_services(backlog, monkeypatch):
    step = processing_module.StepResult(
        article_id=0, state="MATCHED", pipeline_key="k|m", story_id=1
    )
    process_service = Recorder(step)
    reprocess_service = Recorder(step)
    monkeypatch.setattr(news_story_backlog, "process_article", process_service)
    monkeypatch.setattr(news_story_explain, "reprocess_article", reprocess_service)
    story_id = StoryArticle.objects.get(article_id=backlog["harbor-storm-01"]).story_id
    refreshed = refresh_story(story_id)
    refresh_service = Recorder(refreshed)
    monkeypatch.setattr(news_story, "refresh_story", refresh_service)
    monkeypatch.setattr(news_stories, "refresh_story", refresh_service)

    run("news_story_explain", "--article", str(backlog["rail-strike-01"]), "--reprocess")
    run("news_story", "--story", str(story_id), "--refresh")
    run("news_stories", "--last", "1", "--refresh")
    run("news_story_backlog", "--pending", "--limit", "2", "--process")

    assert reprocess_service.calls == [((backlog["rail-strike-01"],), {})]
    assert refresh_service.calls == [
        ((story_id,), {"reason": "operator"}),
        ((story_id,), {"reason": "operator"}),
    ]
    listed = backlog_rows(run("news_story_backlog", "--pending", "--limit", "2"))
    assert [call[0][0] for call in process_service.calls] == list(listed)


@pytest.mark.django_db
def test_backlog_processing_really_completes_the_listed_articles(backlog):
    output = run("news_story_backlog", "--pending", "--limit", "2", "--process")

    processed = [
        int(line.split()[0].split("=")[1])
        for line in output.splitlines()
        if line.startswith("article_id=")
    ]
    assert len(processed) == 2
    assert (
        "FAILED"
        not in backlog_rows(run("news_story_backlog", "--pending", "--limit", "1000")).values()
    )
    for article_id in processed:
        assert StoryArticle.objects.filter(article_id=article_id, is_primary=True).exists()


# --- the commands contain no Story rule --------------------------------------------------

# Names whose presence in a command would mean it retrieves, matches, embeds,
# enriches or synthesizes by itself instead of calling an application service.
FORBIDDEN_NAMES = {
    "find_candidates",
    "decide_story_match",
    "MatchPolicy",
    "MATCH_POLICY",
    "match_article",
    "embed_article",
    "embed_articles",
    "embed_texts",
    "story_vector",
    "compute_story_embedding",
    "compute_story_enrichment",
    "extract_story_enrichment",
    "compute_story_synthesis",
    "synthesize_story",
    "CosineDistance",
    "RuleBasedEnrichmentExtractor",
    "ExtractiveSynthesizer",
    "configured_provider",
    "NEWS_STORY_MATCH_MAX_DISTANCE",
    "NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS",
    "NEWS_STORY_CANDIDATE_MAX_DISTANCE",
}
ALLOWED_MODULES = ("news.application.", "news.models", "news.tasks", "django.")


@pytest.mark.parametrize("filename", STORY_COMMANDS)
def test_story_commands_contain_no_matching_retrieval_or_enrichment_rule(filename):
    source = (COMMANDS_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)

    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported |= {alias.name for alias in node.names}
            if node.level == 0:
                assert node.module.startswith(ALLOWED_MODULES), node.module
        elif isinstance(node, ast.Import):
            raise AssertionError(f"{filename} imports {[a.name for a in node.names]}")
    assert not (names | imported) & FORBIDDEN_NAMES
    assert "<=>" not in source and "cursor" not in source and ".raw(" not in source
