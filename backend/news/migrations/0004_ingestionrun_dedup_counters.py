from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("news", "0003_alter_article_byline")]

    operations = [
        migrations.RemoveConstraint(
            model_name="ingestionrun",
            name="news_run_counters_nonnegative",
        ),
        migrations.AddField(
            model_name="ingestionrun",
            name="content_duplicates",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionrun",
            name="identity_duplicates",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionrun",
            name="raw_rejected",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionrun",
            name="source_identity_conflicts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="ingestionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("items_received__gte", 0),
                    ("items_rejected__gte", 0),
                    ("raw_created__gte", 0),
                    ("raw_unchanged__gte", 0),
                    ("raw_changed__gte", 0),
                    ("items_processed__gte", 0),
                    ("items_failed__gte", 0),
                    ("identity_duplicates__gte", 0),
                    ("content_duplicates__gte", 0),
                    ("raw_rejected__gte", 0),
                    ("source_identity_conflicts__gte", 0),
                ),
                name="news_run_counters_nonnegative",
            ),
        ),
    ]
