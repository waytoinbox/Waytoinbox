from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0055_login_activity'),
    ]

    operations = [
        migrations.CreateModel(
            name='CampaignTestSend',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recipients',  models.JSONField()),
                ('sender_name', models.CharField(blank=True, default='', max_length=255)),
                ('from_email',  models.EmailField(blank=True, default='')),
                ('reply_email', models.EmailField(blank=True, default='')),
                ('status',      models.CharField(max_length=10)),
                ('error_log',   models.TextField(blank=True, default='')),
                ('sent_at',     models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='test_sends',
                    to='Email_validate_app.campaign',
                )),
                ('user', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    to='Email_validate_app.usertable',
                )),
                ('template', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='Email_validate_app.usertemplate',
                )),
            ],
            options={'db_table': 'campaign_test_send'},
        ),
    ]
