from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0099_so_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='solist',
            name='tags',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='solist',
            name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('inactive', 'Inactive')],
                default='active', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='solist',
            name='active_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='solist',
            name='unsubscribed_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='solist',
            name='bounced_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
