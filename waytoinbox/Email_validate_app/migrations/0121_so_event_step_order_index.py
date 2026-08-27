# V3.2 — the composite index for the new step_order column, split into its
# own migration from 0120's column adds (see that file's comment).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0120_so_event_tracked_link_step_order'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='soevent',
            index=models.Index(fields=['campaign', 'step_order', 'event_type'], name='so_event_camp_step_type_idx'),
        ),
    ]
