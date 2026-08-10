from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0095_guest_activity'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='campaign_lists',
            field=models.ManyToManyField(
                blank=True,
                related_name='multi_campaigns',
                to='Email_validate_app.campaignlist',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='campaign_segments',
            field=models.ManyToManyField(
                blank=True,
                related_name='multi_campaigns',
                to='Email_validate_app.segment',
            ),
        ),
    ]
