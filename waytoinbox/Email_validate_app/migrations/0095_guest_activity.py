from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0094_db08b_payment_order_unique'),
    ]

    operations = [
        migrations.CreateModel(
            name='GuestActivity',
            fields=[
                ('id',            models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ip_address',    models.GenericIPAddressField()),
                ('activity_type', models.CharField(choices=[('email_verify', 'Email Verify'), ('dmarc_check', 'DMARC Check')], max_length=20)),
                ('input_value',   models.CharField(max_length=320)),
                ('result',        models.CharField(blank=True, max_length=50)),
                ('created_at',    models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'guest_activity',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='guestactivity',
            index=models.Index(fields=['ip_address', 'activity_type'], name='guest_act_ip_type_idx'),
        ),
        migrations.AddIndex(
            model_name='guestactivity',
            index=models.Index(fields=['created_at'], name='guest_act_created_idx'),
        ),
    ]
