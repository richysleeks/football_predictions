from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('predictions', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='stats_json',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
