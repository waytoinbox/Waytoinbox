from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0089_templateimage'),
    ]

    operations = [
        migrations.AddField(
            model_name='usernotification',
            name='url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
