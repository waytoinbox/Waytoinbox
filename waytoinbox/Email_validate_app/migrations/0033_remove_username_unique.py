from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0032_listfiles_completed_at_listfiles_started_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='usertable',
            name='user_name',
            field=models.CharField(max_length=50),
        ),
    ]
