"""
send_scheduled_campaigns.py
----------------------------
Celery Beat task that fires every minute and dispatches any campaigns whose
schedule_at has passed and whose status is still "scheduled".

Per-campaign Redis locks (key: send_campaign:<id>) prevent two Beat ticks
or two workers from sending the same campaign concurrently.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from Email_validate_app.services.mailer import send_job_failure_alert
from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

# Redis lock TTL — generous upper bound for sending one campaign.
# If the process crashes before the finally block the key self-expires,
# so a subsequent Beat tick can retry the campaign.
_SEND_LOCK_TIMEOUT = 600   # 10 minutes


@shared_task(
    bind=True,
    name="Email_validate_app.tasks.send_scheduled_campaigns.send_campaign_emails_task",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=2000,
    base=LoggedTask,
)
def send_campaign_emails_task(self, campaign_id):
    """
    Async Celery task: sends all emails for a single campaign.

    Flow:
      1. Acquire per-campaign Redis lock  →  skip if already held
      2. Call _send_campaign_emails()     →  existing SES logic, unchanged
      3. Set status = "sent" / "failed"
      4. Trigger immediate CloudWatch sync on success
      5. Release lock in finally block

    Called by both save_campaign() (Send Now) and send_scheduled_campaigns (Beat).
    """
    from Email_validate_app.models import Campaign, CampaignEmail
    from Email_validate_app.services.campaign_sender import send_campaign_emails
    from Email_validate_app.tasks.sync_campaigns_cloudwatch import sync_campaign_events
    from Email_validate_app.services.credit_manager import get_cc_current_credit, deduct_cc_credits

    lock_key = f'send_campaign:{campaign_id}'
    if not cache.add(lock_key, '1', timeout=_SEND_LOCK_TIMEOUT):
        logger.info(
            "send_campaign_emails_task: campaign %s already running — skipping.",
            campaign_id,
        )
        return {'status': 'skipped'}

    try:

        campaign = Campaign.objects.select_related(
            'user', 'template', 'campaign_list'
        ).get(id=campaign_id)

        # Idempotency guard — if a previous attempt already sent this campaign
        # (status update succeeded) or permanently failed it, don't re-send.
        if campaign.status in ('sent', 'failed'):
            logger.info(
                "send_campaign_emails_task: campaign %s already in terminal "
                "state '%s' — skipping (idempotency guard).",
                campaign_id, campaign.status,
            )
            return {'status': campaign.status}

        # CC credit preflight check
        recipient_count = CampaignEmail.objects.filter(
            list_id=campaign.campaign_list_id,
            deleted_at__isnull=True,
            subscribed='subscribed',
        ).count()

        cc_available = get_cc_current_credit(campaign.user_id)
        if cc_available < recipient_count:
            Campaign.objects.filter(id=campaign_id).update(status='failed')
            logger.warning(
                "send_campaign_emails_task: campaign %s blocked — CC credits "
                "insufficient (%d needed, %d available).",
                campaign_id, recipient_count, cc_available,
            )
            try:
                from Email_validate_app.utils import create_notification
                create_notification(campaign.user_id, 'campaign',
                    f'Campaign "{campaign.campaign_name}" failed — insufficient credits',
                    url=f'/Email_Campaigns/campaigns/{campaign.Campaign_ID}/')
            except Exception:
                pass
            try:
                if getattr(campaign.user, 'notify_campaign', True):
                    from Email_validate_app.services.mailer import send_campaign_result_email
                    send_campaign_result_email(
                        user_name=campaign.user.user_name,
                        user_email=campaign.user.user_email,
                        campaign_name=campaign.campaign_name,
                        status='failed',
                    )
            except Exception:
                pass
            return {'status': 'failed', 'reason': 'insufficient_cc_credits'}

        send_count, errors = send_campaign_emails(campaign)

        if send_count > 0:
            # Use .update() instead of .save() — avoids stale-object exceptions
            # that would otherwise trigger a Celery retry and re-send the campaign.
            Campaign.objects.filter(id=campaign_id).update(
                status='sent',
                sent_at=timezone.now(),
                sent_via=getattr(settings, 'EMAIL_PROVIDER', 'ses').lower(),
            )
            logger.info(
                "send_campaign_emails_task: campaign %s sent to %d recipient(s).",
                campaign_id, send_count,
            )
            try:
                from Email_validate_app.utils import create_notification
                create_notification(campaign.user_id, 'campaign',
                    f'Campaign "{campaign.campaign_name}" sent to {send_count:,} recipient(s)',
                    url=f'/Email_Campaigns/campaigns/{campaign.Campaign_ID}/')
            except Exception:
                pass
            try:
                if getattr(campaign.user, 'notify_campaign', True):
                    from Email_validate_app.services.mailer import send_campaign_result_email
                    send_campaign_result_email(
                        user_name=campaign.user.user_name,
                        user_email=campaign.user.user_email,
                        campaign_name=campaign.campaign_name,
                        status='sent',
                        send_count=send_count,
                    )
            except Exception:
                pass
            try:
                deduct_cc_credits(campaign.user_id, send_count,
                                  ref_type='campaign', ref_id=str(campaign.id),
                                  description=f"Campaign send: {campaign.campaign_name}")
            except Exception as cc_exc:
                logger.error(
                    "send_campaign_emails_task: CC credit deduction failed for "
                    "campaign %s: %s", campaign_id, cc_exc,
                )
            try:
                sync_campaign_events(campaign.id)
            except Exception as cw_exc:
                logger.warning(
                    "send_campaign_emails_task: event sync failed for "
                    "campaign %s: %s", campaign_id, cw_exc,
                )
            return {'status': 'sent', 'count': send_count}
        else:
            Campaign.objects.filter(id=campaign_id).update(status='failed')
            logger.error(
                "send_campaign_emails_task: campaign %s failed — no emails sent. "
                "Errors: %s", campaign_id, errors,
            )
            try:
                from Email_validate_app.utils import create_notification
                create_notification(campaign.user_id, 'campaign',
                    f'Campaign "{campaign.campaign_name}" failed — no emails sent',
                    url=f'/Email_Campaigns/campaigns/{campaign.Campaign_ID}/')
            except Exception:
                pass
            try:
                if getattr(campaign.user, 'notify_campaign', True):
                    from Email_validate_app.services.mailer import send_campaign_result_email
                    send_campaign_result_email(
                        user_name=campaign.user.user_name,
                        user_email=campaign.user.user_email,
                        campaign_name=campaign.campaign_name,
                        status='failed',
                    )
            except Exception:
                pass
            return {'status': 'failed', 'errors': errors}

    except Exception as exc:
        logger.exception(
            "send_campaign_emails_task: unexpected error for campaign %s: %s",
            campaign_id, exc,
        )
        if self.request.retries >= self.max_retries:
            # All retries exhausted — mark the campaign as failed.
            send_job_failure_alert(
                "Campaign Sender (send_campaign_emails_task)",
                exc,
                context={"Campaign ID": campaign_id},
            )
            try:
                Campaign.objects.filter(id=campaign_id).update(status='failed')
            except Exception:
                pass
            raise
        logger.warning(
            "send_campaign_emails_task: campaign %s — retry %d/%d in 60s.",
            campaign_id, self.request.retries + 1, self.max_retries,
        )
        raise self.retry(exc=exc, countdown=60)

    finally:
        cache.delete(lock_key)


@shared_task(
    bind=True,
    name="Email_validate_app.tasks.send_scheduled_campaigns.send_scheduled_campaigns",
    max_retries=0,
    ignore_result=False,
    base=LoggedTask,
)
def send_scheduled_campaigns(self):
    """
    Find all scheduled campaigns whose send time has arrived and dispatch each
    as an async send_campaign_emails_task.

    Uses an atomic DB update (scheduled → sending) as the claim instead of a
    Redis lock so the Beat task returns immediately and each campaign is
    processed in its own Celery worker.

    Returns a summary dict: {campaigns_found, dispatched, skipped}.
    """
    from Email_validate_app.models import Campaign

    now = timezone.now()

    campaigns = (
        Campaign.objects
        .filter(
            status='scheduled',
            deleted_at__isnull=True,
            schedule_at__lte=now,
        )
        .order_by('schedule_at')   # dispatch oldest-due first
    )

    total      = campaigns.count()
    dispatched = 0
    skipped    = 0

    logger.info(
        "send_scheduled_campaigns: %d campaign(s) due at %s.",
        total, now.isoformat(),
    )

    for campaign in campaigns:
        # Atomic claim: UPDATE ... WHERE status='scheduled' — prevents a second
        # Beat tick from re-dispatching the same campaign if two ticks overlap.
        claimed = Campaign.objects.filter(
            id=campaign.id, status='scheduled'
        ).update(status='sending')

        if not claimed:
            logger.info(
                "send_scheduled_campaigns: campaign %s already claimed — skipping.",
                campaign.id,
            )
            skipped += 1
            continue

        send_campaign_emails_task.delay(campaign.id)
        dispatched += 1
        logger.info(
            "send_scheduled_campaigns: campaign %s ('%s') dispatched to Celery.",
            campaign.id, campaign.campaign_name,
        )

    summary = {
        'campaigns_found': total,
        'dispatched':      dispatched,
        'skipped':         skipped,
    }
    logger.info("send_scheduled_campaigns: done — %s", summary)
    return summary


@shared_task(
    bind=True,
    name="Email_validate_app.tasks.send_scheduled_campaigns.recover_stuck_campaigns",
    max_retries=0,
    ignore_result=False,
    base=LoggedTask,
)
def recover_stuck_campaigns(self):
    """
    Find campaigns stuck in 'sending' for more than 15 minutes with no Redis lock
    and no sent_at. These are orphaned (Celery worker crashed or was never running).
    Reset them to 'failed' so the user can re-send from the UI.
    """
    from datetime import timedelta
    from Email_validate_app.models import Campaign

    cutoff = timezone.now() - timedelta(minutes=15)
    stuck = Campaign.objects.filter(
        status='sending',
        deleted_at__isnull=True,
        sent_at__isnull=True,
        updated_at__lt=cutoff,
    ).only('id', 'campaign_name')

    recovered = 0
    for campaign in stuck:
        # Only reset if the Celery lock is no longer held (task isn't actively running)
        lock_key = f'send_campaign:{campaign.id}'
        if cache.get(lock_key) is not None:
            logger.info(
                'recover_stuck_campaigns: campaign %s still has active lock — skipping.',
                campaign.id,
            )
            continue

        Campaign.objects.filter(id=campaign.id, status='sending').update(status='failed')
        recovered += 1
        logger.warning(
            'recover_stuck_campaigns: campaign %s ("%s") reset to failed — '
            'was stuck in sending for >15 min with no active worker lock.',
            campaign.id, campaign.campaign_name,
        )

    logger.info('recover_stuck_campaigns: recovered %d campaign(s).', recovered)
    return {'recovered': recovered}
