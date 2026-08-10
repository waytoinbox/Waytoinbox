from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0066_senderemailtoken_softdelete'),
    ]

    operations = [
        migrations.RenameField(
            model_name='campaignlist',
            old_name='unsubscribe_count',
            new_name='neversubscribed_count',
        ),
    ]
