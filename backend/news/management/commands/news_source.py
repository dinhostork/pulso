"""Operator command for News Sources: the manage.py surface, not an HTTP API."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from news.models import Source

from ._operator import resolve_source


class Command(BaseCommand):
    help = "Add, list, enable or disable a News Source."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)

        add = actions.add_parser("add", help="Create a Source.")
        add.add_argument("--slug", required=True, help="Unique identifier, e.g. example-news.")
        add.add_argument("--name", required=True, help="Display name of the publisher.")
        add.add_argument("--homepage-url", default="", help="Optional public homepage URL.")
        add.add_argument(
            "--default-language",
            default="en",
            help="BCP-47 language used when a feed declares none (default: en).",
        )

        actions.add_parser("list", help="List every Source.")
        actions.add_parser("enable", help="Activate a Source.").add_argument("slug")
        actions.add_parser("disable", help="Deactivate a Source.").add_argument("slug")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "add":
            return self._add(options)
        if action == "list":
            return self._list()
        return self._set_active(options["slug"], active=action == "enable")

    def _add(self, options):
        source = Source(
            slug=options["slug"],
            name=options["name"],
            homepage_url=options["homepage_url"],
            default_language=options["default_language"],
        )
        try:
            # Model validation owns the rules, including slug uniqueness and
            # URL syntax; the command only reports them.
            source.full_clean()
        except ValidationError as error:
            raise CommandError(f"Invalid Source: {'; '.join(error.messages)}") from None
        source.save()
        self.stdout.write(f"Created Source {source.pk} {source.slug}")

    def _list(self):
        sources = Source.objects.order_by("slug")
        if not sources:
            self.stdout.write("No Sources configured.")
            return
        self.stdout.write(f"{'ID':>5}  {'SLUG':<24} {'ACTIVE':<7} {'LANG':<6} NAME")
        for source in sources:
            state = "yes" if source.is_active else "no"
            self.stdout.write(
                f"{source.pk:>5}  {source.slug:<24} {state:<7} "
                f"{source.default_language:<6} {source.name}"
            )

    def _set_active(self, slug, *, active):
        source = resolve_source(slug)
        if source.is_active == active:
            self.stdout.write(
                f"Source {source.pk} {source.slug} already {'enabled' if active else 'disabled'}"
            )
            return
        source.is_active = active
        source.save(update_fields=["is_active", "updated_at"])
        self.stdout.write(f"{'Enabled' if active else 'Disabled'} Source {source.pk} {source.slug}")
