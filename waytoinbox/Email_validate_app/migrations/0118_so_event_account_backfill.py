from django.db import migrations


def backfill_sent_delivered_account(apps, schema_editor):
    """One-time historical backfill: for 'sent'/'delivered' SOEvent rows
    created before V2.3.1 added a real `account` FK, recover the sender
    account from the pre-existing metadata['account_id'] value that
    services/so_drip.py::_record_success has always written — but ONLY when
    that account demonstrably belongs to the same user who owns the event's
    own campaign. Never guessed from email/subject/timestamp/sender-name/
    campaign_contact — the only source of truth is this exact metadata key,
    cross-checked against real (SOEmailAccount.user_id ==
    SOCampaign.user_id) ownership. Anything that can't be proven this way —
    a metadata account_id that doesn't exist, or that belongs to a different
    user than the campaign's owner — is left untouched (account stays NULL).

    message_id is never touched here: metadata never carried a message_id
    for these rows, and there is no other deterministic source for it in
    historical data, so it correctly stays '' rather than being fabricated.

    Idempotent by construction: the query is scoped to account__isnull=True,
    so a row this migration (or a prior run of it) already populated can
    never be matched or touched again.
    """
    SOEvent = apps.get_model('Email_validate_app', 'SOEvent')
    SOEmailAccount = apps.get_model('Email_validate_app', 'SOEmailAccount')
    SOCampaign = apps.get_model('Email_validate_app', 'SOCampaign')

    candidates = SOEvent.objects.filter(
        event_type__in=('sent', 'delivered'), account__isnull=True, metadata__has_key='account_id',
    )

    eligible = candidates.count()
    backfilled = 0
    skipped_invalid_account = 0
    skipped_tenant_mismatch = 0
    skipped_missing_campaign = 0

    # A real dataset has very few distinct account_id / campaign_id values
    # relative to event-row count — cache both lookups.
    account_cache = {}
    campaign_owner_cache = {}

    for ev in candidates.iterator():
        raw_account_id = ev.metadata.get('account_id') if isinstance(ev.metadata, dict) else None
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError):
            skipped_invalid_account += 1
            continue

        if account_id not in account_cache:
            account_cache[account_id] = SOEmailAccount.objects.filter(id=account_id).first()
        account = account_cache[account_id]
        if account is None:
            skipped_invalid_account += 1
            continue

        if ev.campaign_id not in campaign_owner_cache:
            camp = SOCampaign.objects.filter(id=ev.campaign_id).values('user_id').first()
            campaign_owner_cache[ev.campaign_id] = camp['user_id'] if camp else None
        campaign_owner_id = campaign_owner_cache[ev.campaign_id]
        if campaign_owner_id is None:
            skipped_missing_campaign += 1
            continue

        if account.user_id != campaign_owner_id:
            skipped_tenant_mismatch += 1
            continue

        # Conditional update (not a blind .save()) — matches this codebase's
        # existing idempotency convention everywhere else, and guarantees an
        # already-populated row is never overwritten even if this were
        # somehow re-entered concurrently.
        updated = SOEvent.objects.filter(id=ev.id, account__isnull=True).update(account_id=account.id)
        if updated:
            backfilled += 1

    print(
        f'\n[0118 SOEvent account backfill] eligible={eligible} backfilled={backfilled} '
        f'skipped_invalid_account={skipped_invalid_account} '
        f'skipped_tenant_mismatch={skipped_tenant_mismatch} '
        f'skipped_missing_campaign={skipped_missing_campaign}'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0117_so_campaign_tracking_toggle'),
    ]

    operations = [
        migrations.RunPython(backfill_sent_delivered_account, migrations.RunPython.noop),
    ]
