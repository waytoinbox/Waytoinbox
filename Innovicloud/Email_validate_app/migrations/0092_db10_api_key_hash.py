import hashlib
from django.db import migrations, models


def populate_key_hash(apps, schema_editor):
    APIKey = apps.get_model('Email_validate_app', 'APIKey')
    for api_key in APIKey.objects.all():
        raw = api_key.key or ''
        api_key.key_hash = hashlib.sha256(raw.encode()).hexdigest()
        api_key.save(update_fields=['key_hash'])


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0091_db07_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='apikey',
            name='key_hash',
            field=models.CharField(max_length=64, null=True, blank=True),
        ),
        migrations.RunPython(populate_key_hash, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='apikey',
            name='key_hash',
            field=models.CharField(max_length=64, unique=True),
        ),
    ]
