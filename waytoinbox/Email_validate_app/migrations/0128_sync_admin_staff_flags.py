from django.db import migrations


def sync_admin_staff_flags(apps, schema_editor):
    UserTable = apps.get_model('Email_validate_app', 'UserTable')
    UserTable.objects.filter(is_admin=True).update(is_staff=True)
    UserTable.objects.filter(is_superuser=True).update(is_staff=True)


class Migration(migrations.Migration):
    dependencies = [
        ('Email_validate_app', '0127_so_condition_groups'),
    ]

    operations = [
        migrations.RunPython(sync_admin_staff_flags, migrations.RunPython.noop),
    ]
