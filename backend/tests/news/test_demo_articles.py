"""The opt-in fictional demo seed (#52): explicit, bounded, idempotent, provenance only."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from news.adapters.deterministic_embeddings import DeterministicEmbeddingProvider
from news.application import embeddings as embeddings_module
from news.models import Article, RawArticle, Source, SourceEndpoint, Story, StoryArticle


def run(*args) -> str:
    out = io.StringIO()
    call_command("news_demo_articles", *args, stdout=out)
    return out.getvalue()


def snapshot():
    return {
        model.__name__: list(model.objects.order_by("pk").values())
        for model in (Source, SourceEndpoint, RawArticle, Article)
    }


@pytest.mark.django_db
def test_dry_run_is_the_default_and_writes_nothing():
    output = run()
    assert "Dry run" in output
    assert "3 missing" in output and "8 missing" in output
    assert not Article.objects.exists() and not Source.objects.exists()


@pytest.mark.django_db
def test_apply_creates_only_missing_fictional_rows_and_is_idempotent():
    run("--apply")
    assert Source.objects.count() == 3 and Article.objects.count() == 8
    assert all(
        slug.startswith("fictional-") for slug in Source.objects.values_list("slug", flat=True)
    )
    assert all(
        ".example/" in url for url in Article.objects.values_list("canonical_url", flat=True)
    )
    endpoints = list(SourceEndpoint.objects.all())
    assert len(endpoints) == 3 and not any(endpoint.is_active for endpoint in endpoints)
    assert not StoryArticle.objects.exists()  # provenance only; Stories come from the pipeline

    before = snapshot()
    assert "(0 missing)" in run("--apply")
    assert snapshot() == before


@pytest.mark.django_db
def test_a_fictional_endpoint_cannot_be_activated():
    run("--apply")
    endpoint = SourceEndpoint.objects.first()
    endpoint.is_active = True
    with pytest.raises(ValidationError):
        endpoint.save()


@pytest.mark.django_db
def test_the_normal_story_pipeline_turns_the_demo_into_stories(monkeypatch):
    monkeypatch.setattr(
        embeddings_module, "embedding_provider_for", lambda _name: DeterministicEmbeddingProvider()
    )
    run("--apply")
    call_command("news_story_reconcile", stdout=io.StringIO())
    call_command("news_story_refresh", "--stale-failed", stdout=io.StringIO())
    assert StoryArticle.objects.filter(is_primary=True).count() == 8
    stories = Story.objects.filter(status=Story.Status.ACTIVE)
    assert stories.exists()
    assert set(stories.values_list("refresh_state", flat=True)) == {Story.RefreshState.CURRENT}
