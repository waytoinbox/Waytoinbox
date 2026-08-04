"""
sync_campaigns_cloudwatch.py
----------------------------
Celery task that runs every 5 minutes via Beat and incrementally synchronises
CloudWatch SES tracking events for all sent campaigns.

Each campaign is processed independently — a failure on one never blocks the
rest.  The existing sync_cloudwatch_for_campaign() function handles duplicate
prevention, per-recipient events, stats recalculation, and last_cloudwatch_sync
stamping, so this task is deliberately thin.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)


def sync_campaign_events(campaign_id):
    """Route event sync to the provider that actually sent the campaign."""
    from Email_validate_app.models import Campaign
    try:
        provider = Campaign.objects.filter(id=campaign_id).values_list('sent_via', flat=True).get()
    except Campaign.DoesNotExist:
        provider = None

    # Fall back to current setting for campaigns sent before sent_via was added
    if not provider:
        provider = getattr(settings, 'EMAIL_PROVIDER', 'ses').lower()

    if provider == 'mailgun':
        from Email_validate_app.tasks.mailgun_sync import sync_mailgun_for_campaign
        return sync_mailgun_for_campaign(campaign_id)
    from Email_validate_app.tasks.cloudwatch_sync import sync_cloudwatch_for_campaign
    return sync_cloudwatch_for_campaign(campaign_id)


@shared_task(
    bind=True,
    name="Email_validate_app.tasks.sync_campaigns_cloudwatch.sync_pending_campaigns",
    max_retries=0,
    ignore_result=False,
    base=LoggedTask,
)
def sync_pending_campaigns(self):
    """
    Find every sent (non-deleted) campaign and synchronise its CloudWatch
    tracking events using the incremental sync_cloudwatch_for_campaign() logic.

    Uses last_cloudwatch_sync to determine the start of each polling window,
    so only new events since the previous run are fetched.

    Returns a summary dict: {campaigns_found, synced, errors}.
    """
    from Email_validate_app.models import Campaign

    campaigns = (
        Campaign.objects
        .filter(
            status='sent',
            deleted_at__isnull=True,
            sent_at__gte=timezone.now() - timedelta(days=30),
        )
        .exclude(
            last_cloudwatch_sync__gte=timezone.now() - timedelta(minutes=5),
        )
        .only('id', 'campaign_name', 'sent_at', 'last_cloudwatch_sync')
        .order_by('sent_at')
    )

    total   = campaigns.count()
    synced  = 0
    errors  = 0

    logger.info(
        "sync_pending_campaigns: starting — %d sent campaign(s) to process.", total
    )

    for campaign in campaigns:
        try:
            sync_campaign_events(campaign.id)
            synced += 1
        except Exception as exc:
            errors += 1
            logger.error(
                "sync_pending_campaigns: failed for campaign %s ('%s'): %s",
                campaign.id, campaign.campaign_name, exc,
            )

    summary = {
        'campaigns_found': total,
        'synced':          synced,
        'errors':          errors,
    }
    logger.info("sync_pending_campaigns: done — %s", summary)
    return summary
