from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0064_sender_domains'),
    ]

    operations = [
        migrations.AddField(
            model_name='senderdomain',
            name='is_hidden',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='deleted_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='senderdomain',
            name='domain',
            field=models.CharField(max_length=255),
        ),
    ]
