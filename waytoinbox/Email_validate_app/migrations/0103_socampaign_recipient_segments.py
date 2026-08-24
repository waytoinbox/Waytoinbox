from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0102_sosegment'),
    ]

    operations = [
        migrations.AddField(
            model_name='socampaign',
            name='recipient_segments',
            field=models.ManyToManyField(
                blank=True, db_table='so_campaign_segments',
                related_name='campaigns', to='Email_validate_app.sosegment'),
        ),
    ]
