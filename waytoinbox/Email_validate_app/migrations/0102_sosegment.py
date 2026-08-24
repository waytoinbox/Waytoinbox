"""Sales Outreach segments.

NOTE: the `so_segments` table already exists on databases that ran the
(since-deleted) 0100_solist_status_sosegment migration. Fake-apply this one
there: `manage.py migrate Email_validate_app 0102_sosegment --fake`.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0101_so_contact_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='SOSegment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, default='')),
                ('status', models.CharField(
                    choices=[('active', 'Active'), ('inactive', 'Inactive')],
                    default='active', max_length=10)),
                ('match_type', models.CharField(
                    choices=[('all', 'All (AND)'), ('any', 'Any (OR)')],
                    default='all', max_length=3)),
                ('rules', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='so_segments', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'so_segments',
                'ordering': ['-created_at'],
            },
        ),
    ]
