import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("news", "0012_story_feed_order_index"),
        ("reading", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FeedImpression",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("original_story_id", models.BigIntegerField()),
                ("event_id", models.UUIDField()),
                ("feed_session_id", models.UUIDField()),
                ("position", models.PositiveIntegerField()),
                ("surface", models.CharField(choices=[("HOME_FEED", "Home Feed")], max_length=16)),
                ("policy_version", models.PositiveSmallIntegerField()),
                ("occurred_at", models.DateTimeField()),
                ("received_at", models.DateTimeField()),
                (
                    "story",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feed_impressions",
                        to="news.story",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feed_impressions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["received_at"], name="reading_impr_received_idx")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "event_id"), name="reading_impression_user_event_unique"
                    ),
                    models.UniqueConstraint(
                        fields=("user", "feed_session_id", "original_story_id"),
                        name="reading_impression_exposure_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("story__isnull", True),
                            ("story", models.F("original_story_id")),
                            _connector="OR",
                        ),
                        name="reading_impression_story_is_original",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("original_story_id__gt", 0)),
                        name="reading_impression_original_story_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("position__lte", 100000)),
                        name="reading_impression_position_bounded",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("surface__in", ["HOME_FEED"])),
                        name="reading_impression_surface_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("policy_version__gte", 1)),
                        name="reading_impression_policy_version_valid",
                    ),
                ],
            },
        ),
    ]
