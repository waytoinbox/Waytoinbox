from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0045_reputationresults_reputation_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='reputation',
            name='is_hidden',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='reputation',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
