"""
cloudwatch_sync.py
------------------
Polls AWS CloudWatch Logs for SES tracking events belonging to a specific
campaign, persists them as CampaignEvent rows (one per recipient per event),
and recalculates CampaignStats from the stored events.

Usage (call once right after a campaign send completes):

    from Email_validate_app.tasks.cloudwatch_sync import sync_cloudwatch_for_campaign
    sync_cloudwatch_for_campaign(campaign.id)
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone as tz

logger = logging.getLogger(__name__)

# CloudWatch Logs group written to by the SES event destination.
_LOG_GROUP = '/aws/events/SES-Tracking-Logs'

# When no last_cloudwatch_sync exists, start this many minutes before sent_at
# to catch events that fired immediately on send.
_PRE_SEND_BUFFER_MINUTES = 5

# Maximum seconds a single per-campaign sync may hold the distributed lock.
# Acts as a safety-net TTL: if the process crashes before the finally block,
# the key expires automatically so the next run is never permanently locked out.
_LOCK_TIMEOUT_SECONDS = 300

# Maps SES eventType values (lowercased) to our CampaignEvent.event_type choices.
_EVENT_TYPE_MAP = {
    'send': 'send',
    'delivery': 'delivery',
    'open': 'open',
    'click': 'click',
    'bounce': 'bounce',
    'complaint': 'complaint',
    'reject': 'reject',
    'unsubscribe': 'unsubscribe',
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_logs_client():
    import boto3  # installed in the deployment env alongside the SES client in views.py
    return boto3.client(
        'logs',
        aws_access_key_id=settings.AWS_SES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SES_SECRET_ACCESS_KEY,
        region_name=settings.AWS_SES_REGION,
    )


def _fetch_log_events(client, campaign_id, start_ms, end_ms):
    """
    Page through CloudWatch Logs filter_log_events for the given campaign_id
    within [start_ms, end_ms] (epoch milliseconds).

    Returns a flat list of raw log-event dicts (each has a 'message' key).
    """
    # Events arrive wrapped in the EventBridge envelope: {"source":"aws.ses","detail":{...}}
    # so the tag path is $.detail.mail.tags.campaign_id[0], not $.mail.tags.campaign_id[0].
    filter_pattern = f'{{$.detail.mail.tags.campaign_id[0] = "{campaign_id}"}}'

    kwargs = {
        'logGroupName': _LOG_GROUP,
        'startTime': start_ms,
        'endTime': end_ms,
        'filterPattern': filter_pattern,
    }

    from botocore.exceptions import ClientError

    events = []
    while True:
        try:
            response = client.filter_log_events(**kwargs)
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code', '')
            if code == 'ResourceNotFoundException':
                logger.warning(
                    "CloudWatch log group '%s' does not exist yet – "
                    "configure the SES event destination first.", _LOG_GROUP
                )
            else:
                logger.error("CloudWatch filter_log_events error: %s", exc)
            break

        events.extend(response.get('events', []))

        next_token = response.get('nextToken')
        if not next_token:
            break
        kwargs['nextToken'] = next_token

    return events


def _parse_event_timestamp(event_dict, event_type_lower, mail):
    """
    Return the most precise tz-aware datetime for an SES event.

    Priority:
      1. Sub-event object timestamp  (e.g. event['delivery']['timestamp'])
      2. mail.timestamp
      3. Current UTC time as fallback
    """
    sub = event_dict.get(event_type_lower)
    ts_str = (sub.get('timestamp') if isinstance(sub, dict) else None) \
             or mail.get('timestamp')

    if ts_str:
        try:
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    return datetime.now(timezone.utc)


def _parse_ses_event(raw_message):
    """
    Parse one CloudWatch Logs message (a JSON string) into a structured dict.

    Handles two formats:
      • Direct SES event JSON (e.g. from SNS):  event.eventType, event.mail
      • EventBridge envelope (written by EventBridge → CWL rule):
            event.source == 'aws.ses', SES payload is in event.detail

    Returns None for any malformed or incomplete record so callers can skip
    safely without crashing.

    Returned dict keys:
        campaign_id  int
        message_id   str
        emails       list[str]   – ALL addresses from mail.destination
        event_type   str         – normalised lowercase value
        event_time   datetime    – tz-aware
        raw          dict        – full parsed event (stored in metadata)
    """
    try:
        event = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError):
        return None

    # Unwrap EventBridge envelope: {"source":"aws.ses","detail-type":"SES Email Event Notification","detail":{...}}
    if event.get('source') == 'aws.ses' and isinstance(event.get('detail'), dict):
        event = event['detail']

    mail = event.get('mail') or {}
    message_id = mail.get('messageId')
    if not message_id:
        return None

    # SES delivers message tags as {"campaign_id": ["42"], "ses:source-ip": [...]}
    tags = mail.get('tags') or {}
    campaign_id_values = tags.get('campaign_id') or []
    try:
        campaign_id = int(campaign_id_values[0])
    except (ValueError, IndexError):
        return None

    # Collect every recipient address – create one CampaignEvent per address.
    emails = [addr for addr in (mail.get('destination') or []) if addr]
    if not emails:
        return None

    raw_type = (event.get('eventType') or '').lower()
    event_type = _EVENT_TYPE_MAP.get(raw_type)
    if not event_type:
        return None

    event_time = _parse_event_timestamp(event, raw_type, mail)

    return {
        'campaign_id': campaign_id,
        'message_id': message_id,
        'emails': emails,
        'event_type': event_type,
        'event_time': event_time,
        'raw': event,
    }


def _recalculate_stats(campaign, stats):
    """
    Recount every event type from CampaignEvent in a single aggregation query
    and overwrite the CampaignStats row.  This guarantees stats always match
    the persisted events, even across multiple sync runs.
    """
    from Email_validate_app.models import CampaignEvent

    # One query: GROUP BY event_type → count
    counts = dict(
        CampaignEvent.objects
        .filter(campaign=campaign)
        .values('event_type')
        .annotate(n=Count('id'))
        .values_list('event_type', 'n')
    )

    stats.total_sent         = counts.get('send', 0)
    stats.total_delivered    = counts.get('delivery', 0)
    stats.total_opened       = counts.get('open', 0)
    stats.total_clicked      = counts.get('click', 0)
    stats.total_bounced      = counts.get('bounce', 0)
    stats.total_complaints   = counts.get('complaint', 0)
    stats.total_rejected     = counts.get('reject', 0)
    stats.total_unsubscribed = counts.get('unsubscribe', 0)
    stats.save()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_cloudwatch_for_campaign(campaign_id):
    """
    Poll CloudWatch Logs for SES tracking events belonging to *campaign_id*,
    persist new CampaignEvent rows (one per recipient, skipping duplicates),
    then recalculate CampaignStats from the stored events.

    Time window:
      • If campaign.last_cloudwatch_sync is set → use it as start_time.
      • Otherwise → use campaign.sent_at - 5 min as start_time.
      • Always use now() as end_time.

    After a successful run, campaign.last_cloudwatch_sync is stamped with the
    end_time used for this poll so the next call never re-scans the same window.
    """
    from django.core.cache import cache
    from Email_validate_app.models import (
        Campaign,
        CampaignEmail,
        CampaignEvent,
        CampaignStats,
    )

    # ── Distributed lock ────────────────────────────────────────────────────
    # cache.add() maps to Redis SET key value NX EX — atomic, returns True only
    # when the key did not already exist.  This prevents two workers (or the
    # immediate post-send call racing against a Beat tick) from processing the
    # same campaign concurrently.
    # The TTL is a crash-safety net: if the process dies before the finally
    # block the key auto-expires so the next run is never permanently locked out.
    lock_key = f'cw_sync:{campaign_id}'
    if not cache.add(lock_key, '1', timeout=_LOCK_TIMEOUT_SECONDS):
        logger.info(
            "sync_cloudwatch_for_campaign: campaign %s is already being "
            "synced by another worker — skipping.", campaign_id,
        )
        return

    try:
        if not (settings.AWS_SES_ACCESS_KEY_ID and settings.AWS_SES_SECRET_ACCESS_KEY):
            logger.warning(
                "AWS credentials are not configured – skipping CloudWatch sync "
                "for campaign %s.", campaign_id
            )
            return

        try:
            campaign = Campaign.objects.select_related('user').get(id=campaign_id)
        except Campaign.DoesNotExist:
            logger.error(
                "sync_cloudwatch_for_campaign: Campaign %s not found.", campaign_id
            )
            return

        # ── Determine polling window ────────────────────────────────────────
        sync_end = tz.now()

        if campaign.last_cloudwatch_sync:
            # Resume from where we last left off — no gap, no overlap.
            sync_start = campaign.last_cloudwatch_sync
        else:
            # First poll: go back 5 minutes before sent_at to capture events
            # that fired the instant SES accepted the messages.
            reference = campaign.sent_at or sync_end
            sync_start = reference - timedelta(minutes=_PRE_SEND_BUFFER_MINUTES)

        start_ms = int(sync_start.timestamp() * 1000)
        end_ms   = int(sync_end.timestamp() * 1000)

        logger.info(
            "CloudWatch sync for campaign %s: window [%s to %s].",
            campaign_id, sync_start.isoformat(), sync_end.isoformat(),
        )

        # ── Build email → CampaignEmail lookup (for the nullable FK) ────────
        email_to_ce = {
            ce.email: ce
            for ce in CampaignEmail.objects.filter(
                list_id=campaign.campaign_list_id,
                deleted_at__isnull=True,
            )
        }

        # ── Fetch & process log events ───────────────────────────────────────
        client = _make_logs_client()
        log_events = _fetch_log_events(client, campaign_id, start_ms, end_ms)

        logger.info(
            "CloudWatch sync: %d raw log event(s) fetched for campaign %s.",
            len(log_events), campaign_id,
        )

        new_event_count = 0

        for log_event in log_events:
            parsed = _parse_ses_event(log_event.get('message', ''))
            if parsed is None:
                continue

            # Guard against filter-pattern false positives (e.g. campaign 1
            # matching campaign 10 or 21 if the pattern ever widens the match).
            if parsed['campaign_id'] != campaign_id:
                continue

            # Create one CampaignEvent row per recipient in this log entry.
            for email in parsed['emails']:
                campaign_email_obj = email_to_ce.get(email)

                try:
                    with transaction.atomic():
                        CampaignEvent.objects.create(
                            user=campaign.user,
                            campaign=campaign,
                            campaign_email=campaign_email_obj,
                            email=email,
                            message_id=parsed['message_id'],
                            event_type=parsed['event_type'],
                            event_time=parsed['event_time'],
                            metadata=parsed['raw'],
                        )
                    new_event_count += 1

                    if parsed['event_type'] == 'unsubscribe' and campaign_email_obj:
                        if campaign_email_obj.subscribed != 'unsubscribed':
                            campaign_email_obj.subscribed = 'unsubscribed'
                            campaign_email_obj.save(update_fields=['subscribed'])

                except IntegrityError:
                    # Duplicate: (message_id, email, event_type) already stored.
                    pass

        # ── Recalculate stats from persisted events ──────────────────────────
        # Always recalculate (even when new_event_count == 0) to self-heal stats
        # that may have drifted from a previous partial run.
        stats, _ = CampaignStats.objects.get_or_create(campaign=campaign)
        _recalculate_stats(campaign, stats)

        # ── Stamp the sync window end so the next call starts from here ──────
        campaign.last_cloudwatch_sync = sync_end
        campaign.save(update_fields=['last_cloudwatch_sync'])

        logger.info(
            "CloudWatch sync complete for campaign %s: %d new event(s) inserted.",
            campaign_id, new_event_count,
        )

    finally:
        cache.delete(lock_key)
