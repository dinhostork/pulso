"""Private, durable reading intent."""

from django.conf import settings
from django.db import models


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
