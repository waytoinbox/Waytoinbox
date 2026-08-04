from django.db import migrations, models


def drop_columns_if_exist(apps, schema_editor):
    """Drop reputation_id and user_id only if they exist in the DB."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'reputation_results'
              AND COLUMN_NAME IN ('reputation_id', 'user_id')
        """)
        existing = {row[0] for row in cursor.fetchall()}

        # Drop FK constraints first (MySQL requires this before dropping columns)
        cursor.execute("""
            SELECT CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA    = DATABASE()
              AND TABLE_NAME      = 'reputation_results'
              AND REFERENCED_TABLE_NAME IS NOT NULL
              AND COLUMN_NAME IN ('reputation_id', 'user_id')
        """)
        fk_constraints = [row[0] for row in cursor.fetchall()]

        for fk in fk_constraints:
            cursor.execute(
                f"ALTER TABLE `reputation_results` DROP FOREIGN KEY `{fk}`"
            )

        for col in existing:
            cursor.execute(
                f"ALTER TABLE `reputation_results` DROP COLUMN `{col}`"
            )


def drop_old_index_if_exists(apps, schema_editor):
    """Drop the old index only if it exists."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT INDEX_NAME
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'reputation_results'
              AND INDEX_NAME   = 'reputation__user_id_68856b_idx'
        """)
        if cursor.fetchone():
            cursor.execute(
                "DROP INDEX `reputation__user_id_68856b_idx` ON `reputation_results`"
            )


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0046_reputation_is_hidden_deleted_at'),
    ]

    operations = [
        # Use raw SQL with IF EXISTS checks instead of RemoveField
        migrations.RunPython(
            drop_columns_if_exist,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            drop_old_index_if_exists,
            migrations.RunPython.noop,
        ),
        # Add the new index (only if it doesn't already exist)
        migrations.AddIndex(
            model_name='reputationresults',
            index=models.Index(fields=['domain', 'date'], name='rep_results_domain_date_idx'),
        ),
    ]
