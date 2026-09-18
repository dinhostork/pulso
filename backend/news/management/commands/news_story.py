"""Inspect one Story: members, Topics, Entities and synthesis provenance (#33)."""

from django.core.management.base import BaseCommand

from news.application.story_inspection import live_member_counts, story_detail
from news.application.story_refresh import refresh_story

from ._operator import bounded_limit, refresh_line, resolve_story, stamp

DEFAULT_MEMBERS = 100


def _distance(value) -> str:
    return "-" if value is None else f"{value:.6f}"


class Command(BaseCommand):
    help = (
        "Show one Story's lifecycle, counters, member Articles, Topics, Entities and "
        "which Articles support its current synthesis. Never prints body text or synthesis text."
    )

    def add_arguments(self, parser):
        parser.add_argument("--story", required=True, help="Story id.")
        parser.add_argument(
            "--members",
            type=int,
            default=DEFAULT_MEMBERS,
            help=f"How many members to list, 1-1000 (default: {DEFAULT_MEMBERS}).",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Refresh this Story through the refresh service before showing it.",
        )

    def handle(self, *args, **options):
        story_id = resolve_story(options["story"])
        member_limit = bounded_limit(options["members"], "--members")
        if options["refresh"]:
            self.stdout.write(refresh_line(refresh_story(story_id, reason="operator")))
        detail = story_detail(story_id, member_limit=member_limit)
        story = detail.story
        live_articles, live_sources = live_member_counts(story_id)
        write = self.stdout.write

        write(
            f"story_id={story.pk} status={story.status} language={story.language} "
            f"created_at={stamp(story.created_at)}"
        )
        write(
            f"refresh_state={story.refresh_state} refreshed_at={stamp(story.refreshed_at)} "
            f"failed_step={detail.failed_step or '-'} error_kind={detail.error_kind or '-'} "
            f"member_signature={story.member_signature or '-'}"
        )
        write(
            f"article_count={story.article_count} source_count={story.source_count} "
            f"live_articles={live_articles} live_sources={live_sources} "
            f"first_published_at={stamp(story.first_published_at)} "
            f"last_published_at={stamp(story.last_published_at)}"
        )
        write(f"embedding_model_keys={','.join(detail.embedding_model_keys) or '-'}")

        shown = len(detail.members)
        write(f"members={detail.member_total} shown={shown}")
        if detail.members:
            write(
                f"  {'ARTICLE':<8}  {'SOURCE':<16}  {'PUBLISHED_AT':<25}  {'METHOD':<13}  "
                f"{'PRIMARY':<7}  {'DISTANCE':<8}  {'CITED':<5}  CANONICAL_URL / TITLE"
            )
        for member in detail.members:
            write(
                f"  {member.article_id:<8}  {member.source_slug:<16}  "
                f"{stamp(member.event_time):<25}  {member.method:<13}  "
                f"{'yes' if member.is_primary else 'no':<7}  {_distance(member.distance):<8}  "
                f"{'yes' if member.cited else 'no':<5}  {member.canonical_url}"
            )
            write(f"  {'':<8}  title: {member.title}")

        write(f"topics={len(detail.topics)}")
        for slug, label, score, model_key in detail.topics:
            write(f"  {slug}  label={label!r}  score={score:.3f}  model_key={model_key}")
        write(f"entities={len(detail.entities)}")
        for kind, name, score, model_key in detail.entities:
            write(f"  {kind:<12}  {name!r}  score={score:.3f}  model_key={model_key}")

        synthesis = detail.synthesis
        if synthesis is None:
            write("synthesis=none")
            return
        matches = synthesis.member_signature == story.member_signature
        write(
            f"synthesis_id={synthesis.synthesis_id} model_key={synthesis.model_key} "
            f"generated_at={stamp(synthesis.generated_at)} "
            f"member_signature={synthesis.member_signature} "
            f"matches_story_signature={'yes' if matches else 'no'}"
        )
        write(
            "cited_articles="
            + (",".join(str(pk) for pk in sorted(synthesis.cited_article_ids)) or "-")
        )
        for element in synthesis.elements:
            write(
                f"  {element.kind}[{element.position}]  supporting_articles="
                + ",".join(str(pk) for pk in element.article_ids)
            )
