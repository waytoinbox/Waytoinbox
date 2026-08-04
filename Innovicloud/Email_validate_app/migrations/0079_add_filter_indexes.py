from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0078_senderdomain_provider'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='emailvalidate',
            index=models.Index(
                fields=['user', 'insert_date', 'is_hidden'],
                name='ev_user_date_hidden_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='campaign',
            index=models.Index(
                fields=['user', 'status', 'created_at'],
                name='camp_user_status_date_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='campaignemail',
            index=models.Index(
                fields=['list', 'subscribed', 'created_at'],
                name='ce_list_sub_date_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='reputation',
            index=models.Index(
                fields=['user', 'status', 'created_at'],
                name='rep_user_status_date_idx',
            ),
        ),
    ]
