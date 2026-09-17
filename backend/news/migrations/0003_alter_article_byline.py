from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("news", "0002_ingestionrun_rawarticle_ingestion_run_and_more")]

    operations = [
        migrations.AlterField(
            model_name="article",
            name="byline",
            field=models.CharField(blank=True, max_length=512),
        ),
    ]
