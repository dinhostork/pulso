"""Drive the complete v0.3 Story pipeline over the #26 corpus (#34).

Only `news.application` services are called: `process_article` (#29: embed,
retrieve, decide, associate) and `refresh_story` (#32: Story embedding, Topics,
Entities, synthesis, counters). Nothing here writes a Story, an association or
a derived row itself.

`run_pipeline` processes Articles in publication order and, after each one,
refreshes every Story the association marked STALE, as the after-commit
`refresh_story_task` dispatch would when the worker keeps up with ingestion.
That ordering is the documented deterministic interleaving for these tests;
the concurrency tests cover the others.

The configured embedding provider is replaced by `RecordedEmbeddingProvider`,
which replays the pinned local model's vectors offline under the real
`model_key`: the hashing double cannot express the corpus scenarios (see
`recorded_embeddings.py`). Extraction and synthesis use the shipped offline
defaults (rule-based extractor, extractive synthesizer).
"""

from django.db.models import Count

from news.application import embeddings as embeddings_module
from news.application.story_processing import process_article
from news.application.story_refresh import refresh_candidates, refresh_story
from news.domain.stories import member_signature
from news.models import (
    Article,
    ArticleEmbedding,
    ArticleStoryProcessing,
    IngestionRun,
    RawArticle,
    Source,
    SourceEndpoint,
    Story,
    StoryArticle,
    StoryEmbedding,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
    StoryTopic,
)
from tests.news.recorded_embeddings import RecordedEmbeddingProvider
from tests.news.story_metrics import evaluate, primary_assignments

MODEL_KEY = RecordedEmbeddingProvider.identity.model_key
PROVENANCE_MODELS = (Article, RawArticle, IngestionRun, Source, SourceEndpoint)
DERIVED_MODELS = (
    Story,
    StoryArticle,
    StoryEmbedding,
    StoryTopic,
    StoryEntity,
    StorySynthesis,
    StorySynthesisElement,
    StorySynthesisElementSource,
    ArticleEmbedding,
    ArticleStoryProcessing,
)
MAX_SETTLE_ROUNDS = 5


def use_recorded_provider(monkeypatch) -> None:
    """Make the configured provider replay the recorded local-model vectors."""

    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: RecordedEmbeddingProvider()
    )


def publication_order(loaded) -> list[int]:
    records = sorted(
        loaded.corpus.articles, key=lambda record: (record.published_offset_minutes, record.id)
    )
    return [loaded.article_ids[record.id] for record in records]


def settle() -> list:
    """Refresh every STALE or FAILED Story until none is left; returns the results."""

    results = []
    for _ in range(MAX_SETTLE_ROUNDS):
        story_ids = refresh_candidates(limit=1000)
        if not story_ids:
            return results
        results.extend(refresh_story(story_id, reason="membership_added") for story_id in story_ids)
    raise AssertionError(f"Stories still need refresh after {MAX_SETTLE_ROUNDS} rounds")


def run_pipeline(article_ids) -> None:
    """Process each Article, then let its Story refresh complete, in the given order."""

    for article_id in article_ids:
        result = process_article(article_id)
        assert result.state == ArticleStoryProcessing.State.MATCHED, result
        settle()


def grouping(loaded) -> set[frozenset[str]]:
    """The active primary partition, excluding archived historical Stories."""

    names = loaded.names
    groups: dict[int, set[str]] = {}
    for article_id, story_id in StoryArticle.objects.filter(
        is_primary=True, story__status=Story.Status.ACTIVE
    ).values_list("article_id", "story_id"):
        groups.setdefault(story_id, set()).add(names[article_id])
    return {frozenset(group) for group in groups.values()}


def grouping_diagnostics(loaded) -> str:
    """Ground truth -> fixture Articles -> actual Story ids, plus offending pairs."""

    assigned = primary_assignments(loaded.article_ids.values())
    report = evaluate(loaded.expected_events, assigned, names=loaded.names)
    lines = [report.describe()]
    for record in sorted(loaded.corpus.articles, key=lambda row: (row.expected_event, row.id)):
        lines.append(
            f"{record.expected_event} -> {record.id} -> story {assigned[loaded.article_ids[record.id]]}"
        )
    return "\n".join(lines)


def story_of(loaded, fixture_id: str) -> Story:
    return Story.objects.get(
        story_articles__article_id=loaded.article_ids[fixture_id],
        story_articles__is_primary=True,
    )


def provenance() -> dict[str, list[dict]]:
    """Every column of every News Core provenance row, in primary-key order."""

    return {
        model.__name__: list(model.objects.order_by("pk").values()) for model in PROVENANCE_MODELS
    }


def row_counts() -> dict[str, int]:
    counts = {model.__name__: model.objects.count() for model in DERIVED_MODELS}
    counts["active Story"] = Story.objects.filter(status=Story.Status.ACTIVE).count()
    # Historical synthesis generations are counted separately from the current ones.
    counts["current StorySynthesis"] = StorySynthesis.objects.filter(is_current=True).count()
    counts[f"StoryEmbedding {MODEL_KEY}"] = StoryEmbedding.objects.filter(
        model_key=MODEL_KEY
    ).count()
    return counts


def duplicate_rows() -> dict[str, int]:
    """Rows that would break a per-Story uniqueness if a constraint ever slipped."""

    def dupes(queryset, *fields):
        return queryset.values(*fields).annotate(n=Count("pk")).filter(n__gt=1).count()

    return {
        "StoryArticle(story, article)": dupes(StoryArticle.objects, "story", "article"),
        "primary StoryArticle(article)": dupes(
            StoryArticle.objects.filter(is_primary=True), "article"
        ),
        "StoryTopic(story, topic)": dupes(StoryTopic.objects, "story", "topic"),
        "StoryEntity(story, entity)": dupes(StoryEntity.objects, "story", "entity"),
        "StoryEmbedding(story, model_key)": dupes(StoryEmbedding.objects, "story", "model_key"),
        "current StorySynthesis(story)": dupes(
            StorySynthesis.objects.filter(is_current=True), "story"
        ),
    }


def live_signature(story_id: int) -> str:
    return member_signature(
        Article.objects.filter(story_articles__story_id=story_id).values_list("pk", "updated_at")
    )


def incoherent_stories() -> list[str]:
    """Every ACTIVE Story must be one CURRENT generation of its live membership."""

    problems = []
    for story in Story.objects.filter(status=Story.Status.ACTIVE).order_by("pk"):
        signature = live_signature(story.pk)
        members = StoryArticle.objects.filter(story=story)
        checks = {
            "nonempty": members.exists(),
            "refresh_state": story.refresh_state == Story.RefreshState.CURRENT,
            "member_signature": story.member_signature == signature,
            "article_count": story.article_count == members.count(),
            "source_count": story.source_count
            == members.values("article__source").distinct().count(),
            "embedding": StoryEmbedding.objects.filter(
                story=story, model_key=MODEL_KEY, member_signature=signature
            ).count()
            == 1,
            "synthesis": StorySynthesis.objects.filter(
                story=story, is_current=True, member_signature=signature
            ).count()
            == 1,
            "topics": not StoryTopic.objects.filter(story=story)
            .exclude(member_signature=signature)
            .exists(),
            "entities": not StoryEntity.objects.filter(story=story)
            .exclude(member_signature=signature)
            .exists(),
        }
        problems.extend(f"story {story.pk}: {name}" for name, ok in checks.items() if not ok)
    return problems


def derived_state(loaded) -> dict:
    """Story-derived state keyed by fixture ids, comparable across separate runs."""

    names = loaded.names
    state = {}
    for story in Story.objects.filter(status=Story.Status.ACTIVE):
        members = frozenset(
            names[pk]
            for pk in StoryArticle.objects.filter(story=story).values_list("article_id", flat=True)
        )
        synthesis = StorySynthesis.objects.get(story=story, is_current=True)
        state[members] = {
            "status": story.status,
            "refresh_state": story.refresh_state,
            "member_signature": story.member_signature,
            "counts": (story.article_count, story.source_count),
            "window": (story.first_published_at, story.last_published_at),
            "associations": sorted(
                (names[row.article_id], row.method, row.is_primary, row.matcher_key)
                for row in StoryArticle.objects.filter(story=story)
            ),
            "embedding": [
                (row.model_key, row.member_count, row.member_signature, list(row.vector))
                for row in StoryEmbedding.objects.filter(story=story)
            ],
            "topics": sorted(
                StoryTopic.objects.filter(story=story).values_list(
                    "topic__slug", "score", "model_key", "member_signature"
                )
            ),
            "entities": sorted(
                StoryEntity.objects.filter(story=story).values_list(
                    "entity__kind",
                    "entity__normalized_key",
                    "score",
                    "model_key",
                    "member_signature",
                )
            ),
            "synthesis": (
                synthesis.model_key,
                synthesis.member_signature,
                StorySynthesis.objects.filter(story=story, is_current=True).count(),
                sorted(
                    (
                        element.kind,
                        element.position,
                        element.text,
                        tuple(
                            names[pk]
                            for pk in element.sources.order_by("position").values_list(
                                "article_id", flat=True
                            )
                        ),
                    )
                    for element in synthesis.elements.all()
                ),
            ),
        }
    return state


def delete_derived_state() -> None:
    """Remove every Story-derived row, leaving News Core provenance untouched.

    Topic and Entity vocabulary rows are shared reference data and stay.
    """

    StoryArticle.objects.all().delete()
    Story.objects.all().delete()
    ArticleEmbedding.objects.all().delete()
    ArticleStoryProcessing.objects.all().delete()
