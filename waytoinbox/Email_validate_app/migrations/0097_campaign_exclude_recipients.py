from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0096_campaign_multi_recipients'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='exclude_lists',
            field=models.ManyToManyField(
                blank=True,
                related_name='excluded_campaigns',
                to='Email_validate_app.campaignlist',
            ),
        ),
        migrations.AddField(
            model_name='campaign',
            name='exclude_segments',
            field=models.ManyToManyField(
                blank=True,
                related_name='excluded_campaigns',
                to='Email_validate_app.segment',
            ),
        ),
    ]
