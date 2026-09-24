"""Private reading intent and short-retention exposure telemetry."""

from django.conf import settings
from django.db import models
from django.db.models import Q

#: Upper bound of a rendered zero-based feed position (validated in #43).
MAX_IMPRESSION_POSITION = 100_000


class Bookmark(models.Model):
    """One account's durable intent to save one Story."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bookmarks",
    )
    story = models.ForeignKey(
        "news.Story",
        on_delete=models.PROTECT,
        related_name="bookmarks",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "story"],
                name="reading_bookmark_user_story_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "-created_at", "-id"],
                name="reading_saved_order_idx",
            ),
        ]


class FeedImpression(models.Model):
    """One account's client-reported qualified exposure of a Story card (ADR-0012).

    Rows are accepted once and never updated. `story` is nulled by a rare hard
    Story deletion (ADR-0011); `original_story_id` keeps the reported identity
    and, unlike the nullable relation, stays part of the exposure key.
    """

    class Surface(models.TextChoices):
        HOME_FEED = "HOME_FEED"

    # The unique constraints below lead with `user`, so they already serve
    # account deletion; a separate single-column index would only cost writes.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="feed_impressions",
        db_index=False,
    )
    story = models.ForeignKey(
        "news.Story",
        on_delete=models.SET_NULL,
        null=True,
        related_name="feed_impressions",
    )
    original_story_id = models.BigIntegerField()
    event_id = models.UUIDField()
    feed_session_id = models.UUIDField()
    position = models.PositiveIntegerField()
    surface = models.CharField(max_length=16, choices=Surface.choices)
    policy_version = models.PositiveSmallIntegerField()
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_id"],
                name="reading_impression_user_event_unique",
            ),
            models.UniqueConstraint(
                fields=["user", "feed_session_id", "original_story_id"],
                name="reading_impression_exposure_unique",
            ),
            models.CheckConstraint(
                condition=Q(story__isnull=True) | Q(story=models.F("original_story_id")),
                name="reading_impression_story_is_original",
            ),
            models.CheckConstraint(
                condition=Q(original_story_id__gt=0),
                name="reading_impression_original_story_positive",
            ),
            models.CheckConstraint(
                condition=Q(position__lte=MAX_IMPRESSION_POSITION),
                name="reading_impression_position_bounded",
            ),
            models.CheckConstraint(
                condition=Q(surface__in=["HOME_FEED"]),
                name="reading_impression_surface_valid",
            ),
            models.CheckConstraint(
                condition=Q(policy_version__gte=1),
                name="reading_impression_policy_version_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["received_at"], name="reading_impr_received_idx"),
        ]
