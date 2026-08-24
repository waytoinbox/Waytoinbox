"""Sales Outreach sequences: steps + A/B variations.

Every existing campaign with a body is backfilled into one step / one variant so
the new editor can open it. `SOCampaign.subject/preview_text/html_body` are kept
as a mirror of step 1 variation A, which is what lets the existing one-shot
sender keep working unchanged.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_step_one(apps, schema_editor):
    SOCampaign        = apps.get_model('Email_validate_app', 'SOCampaign')
    SOSequenceStep    = apps.get_model('Email_validate_app', 'SOSequenceStep')
    SOSequenceVariant = apps.get_model('Email_validate_app', 'SOSequenceVariant')

    have_steps = set(SOSequenceStep.objects.values_list('campaign_id', flat=True))
    pending = [
        c for c in SOCampaign.objects.exclude(html_body='').only(
            'id', 'subject', 'preview_text', 'html_body',
        )
        if c.id not in have_steps
    ]
    if not pending:
        return

    SOSequenceStep.objects.bulk_create(
        [SOSequenceStep(campaign_id=c.id, order=0, wait_days=0, wait_hours=0) for c in pending],
        batch_size=500,
    )
    step_by_campaign = dict(
        SOSequenceStep.objects.filter(campaign_id__in=[c.id for c in pending], order=0)
        .values_list('campaign_id', 'id')
    )
    SOSequenceVariant.objects.bulk_create(
        [
            SOSequenceVariant(
                step_id=step_by_campaign[c.id], label='A', name='Variation 1A',
                subject=c.subject or '', preheader=c.preview_text or '',
                html_body=c.html_body or '',
            )
            for c in pending if c.id in step_by_campaign
        ],
        batch_size=500,
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0103_socampaign_recipient_segments'),
    ]

    operations = [
        migrations.CreateModel(
            name='SOSequenceStep',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField(default=0)),
                ('wait_days', models.PositiveIntegerField(default=0)),
                ('wait_hours', models.PositiveIntegerField(default=0)),
                ('name', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='steps', to='Email_validate_app.socampaign')),
            ],
            options={'db_table': 'so_sequence_steps', 'ordering': ['order']},
        ),
        migrations.CreateModel(
            name='SOSequenceVariant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(default='A', max_length=2)),
                ('name', models.CharField(blank=True, default='', max_length=255)),
                ('subject', models.CharField(blank=True, default='', max_length=500)),
                ('preheader', models.CharField(blank=True, default='', max_length=200)),
                ('html_body', models.TextField(blank=True, default='')),
                ('weight', models.PositiveIntegerField(default=1)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('step', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='variants', to='Email_validate_app.sosequencestep')),
            ],
            options={'db_table': 'so_sequence_variants', 'ordering': ['label']},
        ),
        migrations.AddIndex(
            model_name='sosequencestep',
            index=models.Index(fields=['campaign', 'order'], name='so_seq_step_camp_order_idx'),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='send_mode',
            field=models.CharField(
                choices=[('single', 'Single'), ('sequence', 'Sequence')],
                default='single', max_length=10),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='from_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='reply_to',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='schedule_timezone',
            field=models.CharField(default='Asia/Kolkata', max_length=64),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='exclude_lists',
            field=models.ManyToManyField(
                blank=True, db_table='so_campaign_exclude_lists',
                related_name='excluded_campaigns', to='Email_validate_app.solist'),
        ),
        migrations.AddField(
            model_name='socampaign',
            name='exclude_segments',
            field=models.ManyToManyField(
                blank=True, db_table='so_campaign_exclude_segments',
                related_name='excluded_campaigns', to='Email_validate_app.sosegment'),
        ),
        migrations.RunPython(backfill_step_one, noop),
    ]
