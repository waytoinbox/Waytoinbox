from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """
    Bring campaign_test_send in line with the current CampaignTestSend model.

    The table was originally created with only:
        id, campaign_id, recipients, status, sent_at

    This migration adds the missing columns and makes campaign_id nullable.
    """

    dependencies = [
        ('Email_validate_app', '0058_campaign_list_nullable'),
    ]

    operations = [
        # campaign_id was originally non-nullable — make it nullable
        migrations.AlterField(
            model_name='campaigntestsend',
            name='campaign',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='test_sends',
                to='Email_validate_app.campaign',
            ),
        ),

        # Add user FK
        migrations.AddField(
            model_name='campaigntestsend',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to='Email_validate_app.usertable',
            ),
        ),

        # Add template FK
        migrations.AddField(
            model_name='campaigntestsend',
            name='template',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='Email_validate_app.usertemplate',
            ),
        ),

        # Add sender detail columns
        migrations.AddField(
            model_name='campaigntestsend',
            name='sender_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='campaigntestsend',
            name='from_email',
            field=models.EmailField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='campaigntestsend',
            name='reply_email',
            field=models.EmailField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='campaigntestsend',
            name='error_log',
            field=models.TextField(blank=True, default=''),
        ),
    ]
