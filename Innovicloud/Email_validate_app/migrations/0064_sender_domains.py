from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0063_senderemailtoken_email_unique'),
    ]

    operations = [
        migrations.CreateModel(
            name='SenderDomain',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('domain',      models.CharField(max_length=255, unique=True)),
                ('status',      models.CharField(max_length=20, default='pending',
                                choices=[('pending','Pending'),('verified','Verified'),('failed','Failed')])),
                ('dkim_status', models.CharField(max_length=20, default='NOT_STARTED',
                                choices=[('NOT_STARTED','Not Started'),('PENDING','Pending'),
                                         ('SUCCESS','Success'),('FAILED','Failed'),
                                         ('TEMPORARY_FAILURE','Temporary Failure')])),
                ('dkim_tokens', models.JSONField(blank=True, default=list)),
                ('added_at',    models.DateTimeField(auto_now_add=True)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('user',        models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                to='Email_validate_app.usertable')),
            ],
            options={'db_table': 'sender_domains'},
        ),
    ]
