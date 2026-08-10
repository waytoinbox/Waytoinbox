from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0061_campaign_id_sequential'),
    ]

    operations = [
        migrations.CreateModel(
            name='SenderEmailToken',
            fields=[
                ('id',         models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email',      models.EmailField()),
                ('token',      models.CharField(max_length=64, unique=True)),
                ('confirmed',  models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user',       models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='Email_validate_app.usertable')),
            ],
            options={'db_table': 'sender_email_tokens'},
        ),
    ]
