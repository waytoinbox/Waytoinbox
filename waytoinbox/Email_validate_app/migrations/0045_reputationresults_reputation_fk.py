import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0044_remove_allemails_all_emails_file_id_09f128_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='reputationresults',
            name='reputation',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='results',
                to='Email_validate_app.reputation',
            ),
        ),
    ]
