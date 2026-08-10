from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0053_campaignevent_campaignstats'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='last_cloudwatch_sync',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
