# V3.2 — additive nullable columns only. Kept in its own migration, separate
# from the composite index in 0121, so this stays a low-risk, effectively
# instant metadata-only ALTER TABLE (no default backfill, no table rewrite
# implied) rather than being bundled with the (potentially slower,
# table-scanning) index build.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0119_so_sequence_condition'),
    ]

    operations = [
        migrations.AddField(
            model_name='soevent',
            name='step_order',
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='sotrackedlink',
            name='step_order',
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
    ]
