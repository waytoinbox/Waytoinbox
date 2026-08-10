from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0065_senderdomain_is_hidden'),
    ]

    operations = [
        migrations.AddField(
            model_name='senderemailtoken',
            name='is_hidden',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='senderemailtoken',
            name='deleted_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='senderemailtoken',
            name='email',
            field=models.EmailField(),
        ),
    ]
