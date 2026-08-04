import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0052_campaign_sent_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='CampaignEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254)),
                ('message_id', models.CharField(max_length=255)),
                ('event_type', models.CharField(
                    choices=[
                        ('send', 'Send'),
                        ('delivery', 'Delivery'),
                        ('open', 'Open'),
                        ('click', 'Click'),
                        ('bounce', 'Bounce'),
                        ('complaint', 'Complaint'),
                        ('reject', 'Reject'),
                    ],
                    max_length=20,
                )),
                ('event_time', models.DateTimeField()),
                ('metadata', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='events',
                    to='Email_validate_app.campaign',
                )),
                ('campaign_email', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='Email_validate_app.campaignemail',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='Email_validate_app.usertable',
                )),
            ],
            options={
                'db_table': 'campaign_events',
            },
        ),
        migrations.AddConstraint(
            model_name='campaignevent',
            constraint=models.UniqueConstraint(
                fields=['message_id', 'email', 'event_type'],
                name='unique_campaign_event',
            ),
        ),
        migrations.AddIndex(
            model_name='campaignevent',
            index=models.Index(fields=['campaign', 'event_type'], name='campaign_ev_campaig_idx'),
        ),
        migrations.AddIndex(
            model_name='campaignevent',
            index=models.Index(fields=['message_id'], name='campaign_ev_message_idx'),
        ),
        migrations.CreateModel(
            name='CampaignStats',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('total_sent', models.PositiveIntegerField(default=0)),
                ('total_delivered', models.PositiveIntegerField(default=0)),
                ('total_opened', models.PositiveIntegerField(default=0)),
                ('total_clicked', models.PositiveIntegerField(default=0)),
                ('total_bounced', models.PositiveIntegerField(default=0)),
                ('total_complaints', models.PositiveIntegerField(default=0)),
                ('total_rejected', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('campaign', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='stats',
                    to='Email_validate_app.campaign',
                )),
            ],
            options={
                'db_table': 'campaign_stats',
            },
        ),
    ]
