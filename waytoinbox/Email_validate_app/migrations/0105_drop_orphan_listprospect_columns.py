"""Drop orphan columns on `so_list_prospects`.

`subscribed` and `updated_at` were left behind by a migration that was applied
and then deleted from the repo (see the note in 0102_sosegment.py). Neither is
declared on SOListProspect and no code reads them, but both are NOT NULL with no
default, so under MySQL STRICT_TRANS_TABLES any ORM insert that does not name
them fails with error 1364:

    SOListProspect.objects.get_or_create(...)   -> IntegrityError
    SOListProspect.objects.create(...)          -> IntegrityError
    SOListProspect.objects.bulk_create(...)     -> silently wrote subscribed=''
                                                   (INSERT IGNORE downgrades it)

That broke "Add Prospect" on the list detail page.

Guarded by an information_schema check so this is a no-op on a database built
cleanly from migrations, where the columns never existed.
"""

from django.db import migrations

_TABLE   = 'so_list_prospects'
_ORPHANS = ('subscribed', 'updated_at')


def _existing_columns(schema_editor, table):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'SELECT COLUMN_NAME FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s',
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def drop_orphans(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    present = _existing_columns(schema_editor, _TABLE)
    drops = [c for c in _ORPHANS if c in present]
    if not drops:
        return
    clause = ', '.join(f'DROP COLUMN `{c}`' for c in drops)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE `{_TABLE}` {clause}')


def restore_orphans(apps, schema_editor):
    """Recreate the columns (with defaults, so inserts still work)."""
    if schema_editor.connection.vendor != 'mysql':
        return
    present = _existing_columns(schema_editor, _TABLE)
    adds = []
    if 'subscribed' not in present:
        adds.append("ADD COLUMN `subscribed` varchar(20) NOT NULL DEFAULT ''")
    if 'updated_at' not in present:
        adds.append('ADD COLUMN `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)')
    if not adds:
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE `{_TABLE}` ' + ', '.join(adds))


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0104_so_sequences'),
    ]

    operations = [
        migrations.RunPython(drop_orphans, restore_orphans),
    ]
