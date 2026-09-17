"""Publication provenance and normalized Article persistence."""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.db.models import F, Q

from news.adapters.targets import assert_allowed_target
from news.application.ports import FetchError


class Source(models.Model):
    """Identity of a publisher, independent of its ingestion endpoints."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=255)
    homepage_url = models.URLField(blank=True)
    default_language = models.CharField(max_length=35, default="en")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


_SECRET_KEY = re.compile(r"token|key|secret|password|authorization", re.IGNORECASE)
_HTTP_URL = URLValidator(schemes=["http", "https"])


def _secret_keys(value, path=""):
    """Yield config key paths only; values must never reach error messages."""

    if isinstance(value, dict):
        for key, nested in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)):
                yield key_path
            else:
                yield from _secret_keys(nested, key_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _secret_keys(nested, f"{path}[{index}]")


class SourceEndpoint(models.Model):
    """One concrete RSS or JSON Feed location for a publisher."""

    class Kind(models.TextChoices):
        RSS = "RSS", "RSS"
        JSON_FEED = "JSON_FEED", "JSON Feed"

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="endpoints")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    url = models.URLField(unique=True)
    is_active = models.BooleanField(default=True)
    fetch_interval_seconds = models.PositiveIntegerField(default=900)
    adapter_config = models.JSONField(default=dict, blank=True)
    etag = models.CharField(max_length=255, blank=True)
    last_modified = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(fetch_interval_seconds__gt=0), name="news_endpoint_interval_gt_zero"
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        try:
            _HTTP_URL(self.url)
        except ValidationError:
            errors["url"] = "Endpoint URL must use http or https."
        else:
            try:
                assert_allowed_target(
                    self.url, allow_private=settings.NEWS_FETCH_ALLOW_PRIVATE_NETWORKS
                )
            except FetchError as error:
                errors["url"] = f"Endpoint target rejected: {error.kind.value}."
        if not isinstance(self.adapter_config, dict):
            errors["adapter_config"] = "Adapter configuration must be an object."
        else:
            keys = list(_secret_keys(self.adapter_config))
            if keys:
                errors["adapter_config"] = [
                    f"Secret-like configuration key is forbidden: {key}" for key in keys
                ]
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class IngestionRun(models.Model):
    """Operational record of one endpoint fetch and raw-material intake."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Partial"
        NO_CHANGE = "NO_CHANGE", "No change"
        FAILED = "FAILED", "Failed"

    endpoint = models.ForeignKey(
        SourceEndpoint, on_delete=models.PROTECT, related_name="ingestion_runs"
    )
    trigger = models.CharField(max_length=32)
    attempt = models.PositiveIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_kind = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    will_retry = models.BooleanField(default=False)
    items_received = models.PositiveIntegerField(default=0)
    items_rejected = models.PositiveIntegerField(default=0)
    raw_created = models.PositiveIntegerField(default=0)
    raw_unchanged = models.PositiveIntegerField(default=0)
    raw_changed = models.PositiveIntegerField(default=0)
    items_processed = models.PositiveIntegerField(default=0)
    items_failed = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["endpoint", "-started_at"], name="news_run_endpoint_recent_idx"),
            models.Index(fields=["status", "started_at"], name="news_run_status_started_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(attempt__gte=0), name="news_run_attempt_nonnegative"
            ),
            models.CheckConstraint(
                condition=(
                    Q(items_received__gte=0)
                    & Q(items_rejected__gte=0)
                    & Q(raw_created__gte=0)
                    & Q(raw_unchanged__gte=0)
                    & Q(raw_changed__gte=0)
                    & Q(items_processed__gte=0)
                    & Q(items_failed__gte=0)
                ),
                name="news_run_counters_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(duration_ms__isnull=True) | Q(duration_ms__gte=0),
                name="news_run_duration_nonnegative",
            ),
        ]


class RawArticle(models.Model):
    """A received payload revision, distinct from a normalized Article."""

    class ExternalKeyKind(models.TextChoices):
        EXTERNAL_ID = "EXTERNAL_ID", "External ID"
        CANONICAL_URL = "CANONICAL_URL", "Canonical URL"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        REJECTED = "REJECTED", "Rejected"

    class Outcome(models.TextChoices):
        NONE = "", "No outcome"
        ARTICLE_CREATED = "ARTICLE_CREATED", "Article created"
        ARTICLE_UPDATED = "ARTICLE_UPDATED", "Article updated"
        IDENTITY_DUPLICATE = "IDENTITY_DUPLICATE", "Identity duplicate"
        CONTENT_DUPLICATE = "CONTENT_DUPLICATE", "Content duplicate"
        IDENTITY_CONFLICT = "IDENTITY_CONFLICT", "Identity conflict"
        SOURCE_IDENTITY_CONFLICT = "SOURCE_IDENTITY_CONFLICT", "Source identity conflict"

    endpoint = models.ForeignKey(
        SourceEndpoint, on_delete=models.PROTECT, related_name="raw_articles"
    )
    ingestion_run = models.ForeignKey(
        IngestionRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_articles",
    )
    external_key_kind = models.CharField(max_length=16, choices=ExternalKeyKind.choices)
    external_key = models.TextField()
    external_id = models.TextField(blank=True)
    url = models.TextField(blank=True)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="revisions"
    )
    fetched_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, blank=True, default="")
    rejection_reason = models.CharField(max_length=64, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    article = models.ForeignKey(
        "Article", on_delete=models.SET_NULL, null=True, blank=True, related_name="raw_revisions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "external_key_kind", "external_key", "payload_hash"],
                name="news_raw_identity_revision_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["endpoint", "external_key_kind", "external_key"],
                name="news_raw_identity_idx",
            ),
            models.Index(fields=["status", "created_at"], name="news_raw_status_created_idx"),
        ]


class Article(models.Model):
    """One normalized publication attributed to one Source."""

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="articles")
    endpoint = models.ForeignKey(SourceEndpoint, on_delete=models.PROTECT, related_name="articles")
    raw_article = models.ForeignKey(
        RawArticle, on_delete=models.PROTECT, related_name="normalized_articles"
    )
    external_id = models.TextField(blank=True)
    canonical_url = models.URLField(unique=True)
    title = models.TextField()
    description = models.TextField(blank=True)
    body_text = models.TextField(blank=True)
    byline = models.CharField(max_length=512, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    language = models.CharField(max_length=35)
    content_fingerprint = models.CharField(max_length=64, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    first_seen_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="news_article_source_external_id_unique",
            ),
            models.CheckConstraint(
                condition=~Q(canonical_url=""), name="news_article_canonical_url_nonempty"
            ),
            models.CheckConstraint(
                condition=Q(duplicate_of__isnull=True) | ~Q(duplicate_of_id=F("id")),
                name="news_article_not_self_duplicate",
            ),
        ]
