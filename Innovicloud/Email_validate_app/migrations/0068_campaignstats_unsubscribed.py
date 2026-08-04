from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0067_campaignlist_neversubscribed_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaignstats',
            name='total_unsubscribed',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
