from django.db import migrations, models


def backfill_sequential_ids(apps, schema_editor):
    """Replace random Campaign_IDs with sequential values starting at 1000."""
    Campaign = apps.get_model('Email_validate_app', 'Campaign')
    next_id = 1000
    for campaign in Campaign.objects.order_by('id'):
        campaign.Campaign_ID = next_id
        campaign.save(update_fields=['Campaign_ID'])
        next_id += 1


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0060_campaign_campaign_id'),
    ]

    operations = [
        # Change from VARCHAR(5) to INT
        migrations.AlterField(
            model_name='campaign',
            name='Campaign_ID',
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True, unique=True),
        ),
        # Re-assign sequential IDs from 1000
        migrations.RunPython(backfill_sequential_ids, migrations.RunPython.noop),
    ]
