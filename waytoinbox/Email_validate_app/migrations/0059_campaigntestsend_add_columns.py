from django.db import migrations


class Migration(migrations.Migration):
    """
    NO-OP — kept only so migration history and dependency numbering are not
    disturbed for the real database, where this migration is already recorded
    as applied in django_migrations.

    This migration's original operations (AlterField 'campaign' to nullable,
    then AddField 'user', 'template', 'sender_name', 'from_email',
    'reply_email', 'error_log' on CampaignTestSend) were all redundant with
    what 0056_campaign_test_send.py's CreateModel already defines for this
    model — that migration includes every one of those fields, with 'campaign'
    already `blank=True, null=True`, from the moment the table is created.

    Both files were added together in the same commit (see
    0056_campaign_test_send.py and this file's git history), so this was
    never a case of 0056 being edited after the fact — the pair was
    self-consistent only against a pre-existing physical table that already
    had the narrower shape described in the old docstring, never against a
    fresh database built from these migrations in order. Replaying the
    original operations here on an empty database made Django issue
    `ALTER TABLE campaign_test_send ADD COLUMN user_id ...` for a column
    0056 had just created, which MySQL rejects with
    `(1060, "Duplicate column name 'user_id'")` — the exact error that made
    `manage.py test` unable to build an isolated test database for this app.

    Because Django matches an already-applied migration by (app, name), not
    by the content of its operations, emptying this list changes nothing for
    any database where this migration already ran — it is simply never
    replayed there. It only changes what happens when building a database
    from scratch, which is exactly the case that was broken.
    """

    dependencies = [
        ('Email_validate_app', '0058_campaign_list_nullable'),
    ]

    operations = []
