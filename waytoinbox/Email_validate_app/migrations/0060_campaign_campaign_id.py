import random
from django.db import migrations, models


def generate_campaign_ids(apps, schema_editor):
    """Backfill Campaign_ID for all existing campaign rows."""
    Campaign = apps.get_model('Email_validate_app', 'Campaign')
    used = set()
    for campaign in Campaign.objects.filter(Campaign_ID__isnull=True).order_by('id'):
        for _ in range(100):
            candidate = str(random.randint(10000, 99999))
            if candidate not in used:
                used.add(candidate)
                campaign.Campaign_ID = candidate
                campaign.save(update_fields=['Campaign_ID'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0059_campaigntestsend_add_columns'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='Campaign_ID',
            field=models.CharField(blank=True, db_index=True, max_length=5, null=True, unique=True),
        ),
        migrations.RunPython(generate_campaign_ids, migrations.RunPython.noop),
    ]
