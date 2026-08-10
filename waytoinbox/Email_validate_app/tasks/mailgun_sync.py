"""
mailgun_sync.py
---------------
Polls the Mailgun Events API for tracking events belonging to a specific
campaign, persists them as CampaignEvent rows, and recalculates CampaignStats.

Mirrors the structure of cloudwatch_sync.py so both paths stay in sync.
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone as tz

from Email_validate_app.tasks.cloudwatch_sync import (
    _recalculate_stats,
    _LOCK_TIMEOUT_SECONDS,
    _PRE_SEND_BUFFER_MINUTES,
)
from Email_validate_app.services.providers.mailgun_provider import MailgunPermanentError

logger = logging.getLogger(__name__)


def sync_mailgun_for_campaign(campaign_id):
    """
    Poll Mailgun Events API for tracking events belonging to *campaign_id*,
    persist new CampaignEvent rows (one per recipient, skipping duplicates),
    then recalculate CampaignStats from the stored events.

    Uses the same last_cloudwatch_sync field as the SES path so the
    incremental-window logic is shared.
    """
    from django.core.cache import cache
    from Email_validate_app.models import (
        Campaign,
        CampaignEmail,
        CampaignEvent,
        CampaignStats,
    )
    from Email_validate_app.services.providers import get_provider

    lock_key = f'mg_sync:{campaign_id}'
    if not cache.add(lock_key, '1', timeout=_LOCK_TIMEOUT_SECONDS):
        logger.info(
            'sync_mailgun_for_campaign: campaign %s already syncing — skipping.',
            campaign_id,
        )
        return

    try:
        try:
            campaign = Campaign.objects.select_related('user').get(id=campaign_id)
        except Campaign.DoesNotExist:
            logger.error('sync_mailgun_for_campaign: Campaign %s not found.', campaign_id)
            return

        sync_end = tz.now()
        if campaign.last_cloudwatch_sync:
            sync_start = campaign.last_cloudwatch_sync
        else:
            reference  = campaign.sent_at or sync_end
            sync_start = reference - timedelta(minutes=_PRE_SEND_BUFFER_MINUTES)

        logger.info(
            'Mailgun sync for campaign %s: window [%s to %s].',
            campaign_id, sync_start.isoformat(), sync_end.isoformat(),
        )

        email_to_ce = {
            ce.email: ce
            for ce in CampaignEmail.objects.filter(
                list_id=campaign.campaign_list_id,
                deleted_at__isnull=True,
            )
        }

        provider = get_provider()
        try:
            raw_events = provider.fetch_campaign_events(campaign_id, sync_start, sync_end)
        except MailgunPermanentError as exc:
            # Domain doesn't exist in Mailgun — campaign was sent via a different provider.
            # Stamp the sync time so Beat never retries this campaign via Mailgun.
            logger.warning(
                'Mailgun sync SKIPPED (permanent) for campaign %s — %s. '
                'Stamping last_cloudwatch_sync to suppress future retries.',
                campaign_id, exc,
            )
            campaign.last_cloudwatch_sync = sync_end
            campaign.save(update_fields=['last_cloudwatch_sync'])
            return
        except RuntimeError as exc:
            logger.error(
                'Mailgun sync ABORTED for campaign %s — transient API error: %s. '
                'last_cloudwatch_sync NOT advanced so next run re-scans this window.',
                campaign_id, exc,
            )
            return

        logger.info(
            'Mailgun sync: %d event(s) fetched for campaign %s.',
            len(raw_events), campaign_id,
        )

        new_event_count = 0
        for ev in raw_events:
            email              = ev.get('email', '')
            campaign_email_obj = email_to_ce.get(email)
            try:
                with transaction.atomic():
                    CampaignEvent.objects.create(
                        user=campaign.user,
                        campaign=campaign,
                        campaign_email=campaign_email_obj,
                        email=email,
                        message_id=ev.get('message_id', ''),
                        event_type=ev['event_type'],
                        event_time=ev['timestamp'],
                        metadata=ev.get('raw', {}),
                    )
                new_event_count += 1

                if ev['event_type'] == 'unsubscribe' and campaign_email_obj:
                    if campaign_email_obj.subscribed != 'unsubscribed':
                        campaign_email_obj.subscribed = 'unsubscribed'
                        campaign_email_obj.save(update_fields=['subscribed'])

            except IntegrityError:
                pass  # duplicate (message_id, email, event_type)

        stats, _ = CampaignStats.objects.get_or_create(campaign=campaign)
        _recalculate_stats(campaign, stats)

        # Only advance the window after a successful API fetch
        campaign.last_cloudwatch_sync = sync_end
        campaign.save(update_fields=['last_cloudwatch_sync'])

        logger.info(
            'Mailgun sync complete for campaign %s: %d new event(s) inserted.',
            campaign_id, new_event_count,
        )

    finally:
        cache.delete(lock_key)
