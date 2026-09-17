"""Operator command for News source endpoints, using the model's own validation."""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from news.models import SourceEndpoint

from ._operator import resolve_endpoint, resolve_source


class Command(BaseCommand):
    help = "Add, list, enable or disable a News source endpoint."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)

        add = actions.add_parser("add", help="Create an endpoint for a Source.")
        add.add_argument("--source", required=True, help="Slug of the owning Source.")
        add.add_argument(
            "--kind",
            required=True,
            choices=[choice for choice, _ in SourceEndpoint.Kind.choices],
            help="Syndication format of the endpoint.",
        )
        add.add_argument("--url", required=True, help="Absolute http(s) feed URL.")
        add.add_argument(
            "--interval",
            type=int,
            default=900,
            help="Seconds between scheduled fetches (default: 900).",
        )
        add.add_argument(
            "--adapter-config",
            default="{}",
            help="JSON object of adapter options; secret-like keys are rejected.",
        )

        listing = actions.add_parser("list", help="List endpoints.")
        listing.add_argument("--source", default="", help="Limit to one Source slug.")

        actions.add_parser("enable", help="Activate an endpoint.").add_argument("endpoint")
        actions.add_parser("disable", help="Deactivate an endpoint.").add_argument("endpoint")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "add":
            return self._add(options)
        if action == "list":
            return self._list(options["source"])
        return self._set_active(options["endpoint"], active=action == "enable")

    def _add(self, options):
        source = resolve_source(options["source"])
        try:
            adapter_config = json.loads(options["adapter_config"])
        except json.JSONDecodeError:
            raise CommandError("--adapter-config must be valid JSON") from None
        if not isinstance(adapter_config, dict):
            raise CommandError("--adapter-config must be a JSON object")

        endpoint = SourceEndpoint(
            source=source,
            kind=options["kind"],
            url=options["url"],
            fetch_interval_seconds=options["interval"],
            adapter_config=adapter_config,
        )
        try:
            # full_clean() runs field, constraint and uniqueness checks plus the
            # model's own clean(): scheme, target policy and the rejection of
            # secret-like adapter_config keys (#11, #13). Never bypassed here.
            endpoint.full_clean()
            endpoint.save()
        except ValidationError as error:
            raise CommandError(f"Invalid endpoint: {'; '.join(error.messages)}") from None
        self.stdout.write(
            f"Created SourceEndpoint {endpoint.pk} {endpoint.kind} {endpoint.url} "
            f"for Source {source.slug}"
        )

    def _list(self, source_slug):
        endpoints = SourceEndpoint.objects.select_related("source").order_by("pk")
        if source_slug:
            endpoints = endpoints.filter(source=resolve_source(source_slug))
        if not endpoints:
            self.stdout.write("No endpoints configured.")
            return
        self.stdout.write(
            f"{'ID':>5}  {'SOURCE':<20} {'KIND':<10} {'ACTIVE':<7} {'INTERVAL':>8}  URL"
        )
        for endpoint in endpoints:
            active = "yes" if endpoint.is_active and endpoint.source.is_active else "no"
            self.stdout.write(
                f"{endpoint.pk:>5}  {endpoint.source.slug:<20} {endpoint.kind:<10} "
                f"{active:<7} {endpoint.fetch_interval_seconds:>8}  {endpoint.url}"
            )

    def _set_active(self, identifier, *, active):
        endpoint = resolve_endpoint(identifier)
        if endpoint.is_active == active:
            self.stdout.write(
                f"SourceEndpoint {endpoint.pk} already {'enabled' if active else 'disabled'}"
            )
            return
        # A targeted update: flipping a flag must not re-run URL/target
        # validation (which resolves DNS) for an endpoint whose URL is not
        # changing, and must not touch any other column.
        SourceEndpoint.objects.filter(pk=endpoint.pk).update(
            is_active=active, updated_at=timezone.now()
        )
        self.stdout.write(
            f"{'Enabled' if active else 'Disabled'} SourceEndpoint {endpoint.pk} {endpoint.url}"
        )
