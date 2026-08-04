from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0077_segment'),
    ]

    operations = [
        migrations.AddField(
            model_name='senderdomain',
            name='provider',
            field=models.CharField(default='ses', max_length=20),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='migration_attempted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
