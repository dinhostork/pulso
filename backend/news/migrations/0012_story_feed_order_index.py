from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("news", "0011_widen_matcher_key")]

    operations = [
        migrations.AddIndex(
            model_name="story",
            index=models.Index(
                fields=["status", "-created_at", "-id"],
                name="news_story_feed_order_idx",
            ),
        ),
    ]
