from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0070_campaignemail_subscribed_to_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='schedule_timezone',
            field=models.CharField(default='Asia/Kolkata', max_length=64, blank=True),
        ),
    ]
