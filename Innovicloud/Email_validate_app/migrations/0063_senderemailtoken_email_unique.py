from django.db import migrations, models


def deduplicate_emails(apps, schema_editor):
    """Keep only the most recent row per email, delete older duplicates."""
    SenderEmailToken = apps.get_model('Email_validate_app', 'SenderEmailToken')
    seen = {}
    for obj in SenderEmailToken.objects.order_by('id'):
        email = obj.email
        if email in seen:
            seen[email].delete()   # delete the older one
        seen[email] = obj


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0062_sender_email_tokens'),
    ]

    operations = [
        migrations.RunPython(deduplicate_emails, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='senderemailtoken',
            name='email',
            field=models.EmailField(unique=True),
        ),
    ]
