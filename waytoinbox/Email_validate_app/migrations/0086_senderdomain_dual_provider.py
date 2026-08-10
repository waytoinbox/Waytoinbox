"""
Add per-provider status fields to SenderDomain for dual-provider (SES + Mailgun)
registration. Removes migration_attempted_at and provider fields.
Existing rows are backfilled based on their old provider value.
"""
from django.db import migrations, models


def backfill_provider_status(apps, schema_editor):
    SenderDomain = apps.get_model('Email_validate_app', 'SenderDomain')
    for d in SenderDomain.objects.all():
        provider = getattr(d, 'provider', 'ses') or 'ses'
        if provider == 'mailgun':
            d.mailgun_status      = d.status
            d.mailgun_dkim_tokens = d.dkim_tokens
            d.mailgun_verified_at = d.verified_at
        else:
            d.ses_status      = d.status
            d.ses_dkim_tokens = d.dkim_tokens
            d.ses_verified_at = d.verified_at
        d.save(update_fields=[
            'ses_status', 'ses_dkim_tokens', 'ses_verified_at',
            'mailgun_status', 'mailgun_dkim_tokens', 'mailgun_verified_at',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0085_emailheader_add_analysis_fields'),
    ]

    operations = [
        # Add per-provider fields
        migrations.AddField(
            model_name='senderdomain',
            name='ses_status',
            field=models.CharField(max_length=20, default='pending'),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='ses_dkim_tokens',
            field=models.JSONField(default=list, blank=True),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='ses_verified_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='mailgun_status',
            field=models.CharField(max_length=20, default='pending'),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='mailgun_dkim_tokens',
            field=models.JSONField(default=list, blank=True),
        ),
        migrations.AddField(
            model_name='senderdomain',
            name='mailgun_verified_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        # Backfill existing rows
        migrations.RunPython(backfill_provider_status, migrations.RunPython.noop),
        # Remove fields no longer needed
        migrations.RemoveField(model_name='senderdomain', name='migration_attempted_at'),
        migrations.RemoveField(model_name='senderdomain', name='provider'),
    ]
