from django.db import migrations, models


def convert_bool_to_status(apps, schema_editor):
    CampaignEmail = apps.get_model('Email_validate_app', 'CampaignEmail')
    CampaignEmail.objects.filter(subscribed=True).update(subscribed='subscribed')
    CampaignEmail.objects.filter(subscribed=False).update(subscribed='never_subscribed')


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0069_rename_subscribe_count_campaignlist_subscribed_count_and_more'),
    ]

    operations = [
        # Step 1: change column type to varchar, keep existing boolean values as strings temporarily
        migrations.AlterField(
            model_name='campaignemail',
            name='subscribed',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('subscribed',       'Subscribed'),
                    ('never_subscribed', 'Never Subscribed'),
                    ('unsubscribed',     'Unsubscribed'),
                ],
                default='subscribed',
            ),
        ),
        # Step 2: convert existing True/False values to new string values
        migrations.RunPython(convert_bool_to_status, migrations.RunPython.noop),
    ]
