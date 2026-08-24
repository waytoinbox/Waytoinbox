"""SOProspect.status adopts the CampaignEmail.subscribed vocabulary, and the
SOList counters mirror CampaignList's.

Existing rows: 'active' -> 'subscribed', 'bounced' -> 'unsubscribed'
(a hard bounce is a do-not-contact signal, and the bounce itself stays recorded
in SOEvent, so no history is lost).
"""

from django.db import migrations, models


def forwards_status(apps, schema_editor):
    SOProspect = apps.get_model('Email_validate_app', 'SOProspect')
    SOProspect.objects.filter(status='active').update(status='subscribed')
    SOProspect.objects.filter(status='bounced').update(status='unsubscribed')


def backwards_status(apps, schema_editor):
    SOProspect = apps.get_model('Email_validate_app', 'SOProspect')
    SOProspect.objects.filter(status='subscribed').update(status='active')


def resync_counts(apps, schema_editor):
    SOList         = apps.get_model('Email_validate_app', 'SOList')
    SOListProspect = apps.get_model('Email_validate_app', 'SOListProspect')
    for list_id in SOList.objects.values_list('id', flat=True).iterator():
        qs = SOListProspect.objects.filter(so_list_id=list_id, prospect__deleted_at__isnull=True)
        SOList.objects.filter(id=list_id).update(
            total_count=qs.count(),
            subscribed_count=qs.filter(prospect__status='subscribed').count(),
            neversubscribed_count=qs.filter(prospect__status='never_subscribed').count(),
            unsubscribed_count=qs.filter(prospect__status='unsubscribed').count(),
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0100_so_list_status'),
    ]

    operations = [
        # ── Prospect status vocabulary ───────────────────────────────────────
        migrations.AlterField(
            model_name='soprospect',
            name='status',
            field=models.CharField(
                choices=[
                    ('subscribed', 'Subscribed'),
                    ('unsubscribed', 'Unsubscribed'),
                    ('never_subscribed', 'Never Subscribed'),
                ],
                default='subscribed', max_length=20,
            ),
        ),
        migrations.RunPython(forwards_status, backwards_status),

        # ── List counters mirror CampaignList ────────────────────────────────
        migrations.RenameField(
            model_name='solist', old_name='active_count', new_name='subscribed_count',
        ),
        migrations.RemoveField(model_name='solist', name='bounced_count'),
        migrations.AddField(
            model_name='solist',
            name='neversubscribed_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(resync_counts, noop),
    ]
