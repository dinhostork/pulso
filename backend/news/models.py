"""Publication provenance and normalized Article persistence."""

import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.db.models import F, Q


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
    # IngestionRun and this row's ingestion_run FK are introduced together in #16.
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
    byline = models.CharField(max_length=255, blank=True)
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
