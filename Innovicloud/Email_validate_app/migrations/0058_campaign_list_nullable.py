from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0057_alter_campaigntestsend_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='campaign',
            name='campaign_list',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to='Email_validate_app.campaignlist',
            ),
        ),
    ]
