"""List recent Stories and their derived state (#33); optionally refresh them."""

from django.core.management.base import BaseCommand, CommandError

from news.application.story_inspection import recent_stories, split_refresh_error
from news.application.story_refresh import refresh_story
from news.models import Story

from ._operator import bounded_limit, refresh_line, stamp

DEFAULT_LAST = 20

COLUMNS = (
    ("ID", 7),
    ("STATUS", 8),
    ("LANG", 5),
    ("ARTS", 4),
    ("SRCS", 4),
    ("REFRESH", 7),
    ("REFRESHED_AT", 25),
    ("FAILED_STEP", 17),
    ("ERROR", 20),
    ("SYNTHESIS_MODEL", 0),
)


class Command(BaseCommand):
    help = "Show recent Stories, newest first, with counters and refresh state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--last",
            type=int,
            default=DEFAULT_LAST,
            help=f"How many Stories to show, 1-1000 (default: {DEFAULT_LAST}).",
        )
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--stale", action="store_true", help="Only STALE Stories.")
        state.add_argument(
            "--failed",
            action="store_true",
            help="Only FAILED Stories; exit 1 if any exist.",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Refresh every listed Story now, through the refresh service.",
        )

    def handle(self, *args, **options):
        last = bounded_limit(options["last"], "--last")
        refresh_state = None
        if options["stale"]:
            refresh_state = Story.RefreshState.STALE
        elif options["failed"]:
            refresh_state = Story.RefreshState.FAILED
        rows = recent_stories(last=last, refresh_state=refresh_state)
        if not rows:
            self.stdout.write("No Stories.")
            return

        self.stdout.write("  ".join(f"{title:<{width}}" for title, width in COLUMNS).rstrip())
        for row in rows:
            step, kind = split_refresh_error(row.refresh_error)
            cells = (
                row.story_id,
                row.status,
                row.language,
                row.article_count,
                row.source_count,
                row.refresh_state,
                stamp(row.refreshed_at),
                step or "-",
                kind or "-",
                row.synthesis_model_key or "-",
            )
            self.stdout.write(
                "  ".join(
                    f"{cell!s:<{width}}" for cell, (_, width) in zip(cells, COLUMNS, strict=True)
                ).rstrip()
            )

        if options["refresh"]:
            for row in rows:
                self.stdout.write(refresh_line(refresh_story(row.story_id, reason="operator")))
            return
        if options["failed"]:
            # A nonzero exit status makes this usable in a monitoring script.
            raise CommandError(f"{len(rows)} FAILED Story refresh(es)")
