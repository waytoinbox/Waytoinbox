"""
so_drip.py
----------
Per-recipient execution for a Sales Outreach multi-step sequence.

This module owns exactly one thing: sending the step a given SOCampaignContact
currently owes, then advancing (or stopping) its sequence state. It calls the
existing SMTP helpers in services/so_smtp.py unchanged — nothing about how a
message is built or delivered is touched here, only which body goes out and what
happens to the contact row afterward.

The Celery side (tasks/so_send_campaign.py::so_dispatch_due_sequence_steps) is
responsible for finding due contacts and claiming them atomically before calling
send_next_step(); this module assumes the caller already holds the claim
(status was flipped active -> sending by a conditional UPDATE).
"""

import hashlib
import logging
import smtplib
import uuid
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.db.models import F
from django.utils.timezone import now

logger = logging.getLogger(__name__)

MAX_ATTEMPTS  = 3
RETRY_DELAY   = timedelta(minutes=15)
WEEKDAY_ABBR  = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']  # Python .weekday() order


def pick_variant_label(campaign_id, email, variants):
    """Deterministic per-recipient A/B pick over a list of active variants.

    Hash-based rather than random so re-running enrollment (e.g. a retried task)
    assigns the same recipient the same variant instead of re-rolling it.
    """
    active = [v for v in variants if v.is_active] or list(variants)
    if not active:
        return 'A'
    if len(active) == 1:
        return active[0].label
    h = int(hashlib.sha256(f'{campaign_id}:{email}'.encode()).hexdigest()[:8], 16)
    weights = [max(1, v.weight) for v in active]
    total   = sum(weights)
    target  = h % total
    acc = 0
    for v, w in zip(active, weights):
        acc += w
        if target < acc:
            return v.label
    return active[-1].label


def stop(cc, reason):
    """Halt a contact's sequence. Idempotent — a conditional UPDATE, not a
    read-modify-write, so concurrent stop signals (reply + manual cancel, etc.)
    can't race each other."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status__in=('active', 'sending'),
    ).update(status='stopped', error=f'stopped: {reason}', next_action_at=None)
    if updated:
        logger.info('so_drip: campaign contact %s (%s) stopped — %s', cc.id, cc.email, reason)
    return bool(updated)


def resume(cc):
    """Mirror of stop() — manually resume a paused contact's sequence. The
    dispatcher picks it back up on its next tick, respecting whatever
    send-window/quota checks already apply; no special-casing needed here."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status='stopped',
    ).update(status='active', next_action_at=now(), error='')
    if updated:
        logger.info('so_drip: campaign contact %s (%s) resumed', cc.id, cc.email)
    return bool(updated)


def stop_all_for_email(user_id, email, reason):
    """Stop every in-flight SOCampaignContact for this email across ALL of
    this user's campaigns — not just the one campaign that detected the
    bounce/complaint. A bounce or complaint is evidence about the address
    itself, not about one particular campaign, so no other currently-running
    campaign of this user's should keep mailing it either. Scoped strictly to
    `user_id` — must never affect a different tenant's campaign contacts."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        campaign__user_id=user_id, email=email, status__in=('active', 'sending'),
    ).update(status='stopped', error=f'stopped: {reason}', next_action_at=None)
    if updated:
        logger.info('so_drip: stopped %s in-flight contact(s) for %s (user %s) — %s',
                    updated, email, user_id, reason)
    return updated


def _resolve_step_and_variant(campaign, cc):
    # active_subsequence_id set = the contact branched off the main sequence
    # (services/so_subsequence.py::branch_contact) — current_step then indexes
    # into that subsequence's own steps instead of campaign.steps.
    step_holder = cc.active_subsequence if cc.active_subsequence_id else campaign
    step = step_holder.steps.filter(order=cc.current_step).prefetch_related('variants').first()
    if step is None:
        return None, None
    variants = list(step.variants.all())
    variant = next((v for v in variants if v.is_active and v.label == cc.variant_label), None)
    if variant is None:
        active = [v for v in variants if v.is_active]
        variant = (active or variants or [None])[0]
    return step, variant


def _get_contact_account(cc):
    """Resolve the sender account for this contact.

    Sticky: returns cc.account if already assigned — every step of a recipient's
    sequence sends from the same account (see models.SOCampaignContact.account).
    Self-heals legacy rows (created before this field existed) by round-robin
    picking from cc.campaign.account_rotations — never the user's full account
    pool, only the accounts actually selected for THIS campaign — and persisting
    the choice so it stays sticky from here on. Returns None if the campaign has
    no live rotation accounts at all.
    """
    from Email_validate_app.models import SOCampaignContact

    if cc.account_id:
        return cc.account

    rotations = list(
        cc.campaign.account_rotations
        .filter(account__deleted_at__isnull=True, account__status='connected')
        .select_related('account')
        .order_by('order')
    )
    if not rotations:
        return None

    # Deterministic by contact id, so re-healing the same row twice (e.g. a
    # retried task) always lands on the same account rather than re-rolling it.
    account = rotations[cc.id % len(rotations)].account
    SOCampaignContact.objects.filter(id=cc.id, account__isnull=True).update(account=account)
    cc.account = account
    return account


def _next_utc_midnight():
    """The next UTC-day boundary — the moment SOEmailAccountDailyUsage resets."""
    today_utc = now().date()
    return datetime.combine(today_utc + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)


def _reserve_quota_slot(account):
    """Atomically claim one of `account`'s remaining sends for today.

    Same conditional-UPDATE claim pattern already used to claim due contacts
    (so_dispatch_due_sequence_steps) — the WHERE clause is evaluated against the
    row's current committed value under MySQL/InnoDB row locking, so two
    concurrent callers for the same account cannot both succeed. Returns whether
    this call claimed a slot.
    """
    from Email_validate_app.models import SOEmailAccountDailyUsage

    today = now().date()
    SOEmailAccountDailyUsage.objects.get_or_create(account=account, date=today, defaults={'sent_count': 0})
    updated = SOEmailAccountDailyUsage.objects.filter(
        account=account, date=today, sent_count__lt=account.daily_limit,
    ).update(sent_count=F('sent_count') + 1)
    return bool(updated)


def _release_quota_slot(account):
    """Give back a slot reserved by _reserve_quota_slot — called only when the
    send attempt that reserved it then failed. daily_limit counts successful
    sends only, so a failed attempt must not permanently consume a slot."""
    from Email_validate_app.models import SOEmailAccountDailyUsage

    today = now().date()
    SOEmailAccountDailyUsage.objects.filter(
        account=account, date=today, sent_count__gt=0,
    ).update(sent_count=F('sent_count') - 1)


def _campaign_tz(campaign):
    from zoneinfo import ZoneInfo, available_timezones

    tz_name = campaign.schedule_timezone
    if tz_name not in available_timezones():
        tz_name = 'UTC'
    return ZoneInfo(tz_name)


def _local_now(campaign):
    return now().astimezone(_campaign_tz(campaign))


def _allowed_weekdays(campaign):
    """Parse send_weekdays into a set of abbreviations, e.g. {'mon','wed'}.

    ''.split(',') yields [''] rather than [] — filtering blank entries is what
    makes the "empty/garbage value -> treat as unrestricted" fallback actually
    reachable in _next_window_start.
    """
    days = {d for d in (campaign.send_weekdays or '').split(',') if d}
    return days or set(WEEKDAY_ABBR)


def _in_send_window(campaign):
    """Is right now, in this campaign's timezone, an allowed day+time to send?

    Sending Days & Hours is a standing constraint on every send this campaign
    makes for its whole life — distinct from schedule_at, which only decides
    when step 1 launches. Defaults (all 7 days, 00:00-23:59:59) are the
    "unrestricted" sentinel, so an untouched campaign is never gated by this.
    """
    local = _local_now(campaign)
    allowed = _allowed_weekdays(campaign)
    if WEEKDAY_ABBR[local.weekday()] not in allowed:
        return False
    t = local.time()
    return campaign.send_hour_start <= t <= campaign.send_hour_end


def _next_window_start(campaign):
    """The next moment (UTC) this campaign is allowed to send, given it isn't
    allowed right now. Scans forward day by day (max 8, i.e. a full week plus
    one) for the next day in send_weekdays, at send_hour_start local time."""
    local = _local_now(campaign)
    allowed = _allowed_weekdays(campaign)

    for delta in range(8):
        d = local.date() + timedelta(days=delta)
        if WEEKDAY_ABBR[d.weekday()] not in allowed:
            continue
        candidate = datetime.combine(d, campaign.send_hour_start, tzinfo=local.tzinfo)
        if candidate > local:
            return candidate.astimezone(dt_timezone.utc)
    # Defensive fallback — unreachable with a non-empty `allowed`, since some
    # day within the next 7 must qualify.
    return now() + timedelta(hours=1)


def send_next_step(cc):
    """Send whatever step `cc` currently owes. Caller must already hold the claim
    (cc.status == 'sending'). Returns True on a successful send."""
    from Email_validate_app.models import SOProspect, SOCampaignContact, SOCampaign
    from Email_validate_app.services.so_smtp import inject_tracking, build_message, open_smtp

    campaign = cc.campaign

    # ── Eligibility re-check — state may have changed since this contact was
    # enrolled or since it was claimed. ──────────────────────────────────────
    if cc.prospect_id:
        still_subscribed = SOProspect.objects.filter(
            id=cc.prospect_id, status='subscribed', deleted_at__isnull=True,
        ).exists()
    else:
        still_subscribed = SOProspect.objects.filter(
            user_id=campaign.user_id, email=cc.email,
            status='subscribed', deleted_at__isnull=True,
        ).exists()
    if not still_subscribed:
        stop(cc, 'not_subscribed')
        return False

    step, variant = _resolve_step_and_variant(campaign, cc)
    if step is None or variant is None:
        # No such step (e.g. deleted after enrollment) — nothing left to send.
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='completed', completed_at=now(), next_action_at=None,
        )
        logger.info('so_drip: campaign contact %s (%s) has no step %s — marking completed',
                    cc.id, cc.email, cc.current_step)
        return False

    if not _in_send_window(campaign):
        next_at = _next_window_start(campaign)
        logger.info(
            'so_drip: campaign %s contact %s outside sending window (%s %s-%s %s), deferred to %s',
            campaign.id, cc.id, campaign.send_weekdays, campaign.send_hour_start,
            campaign.send_hour_end, campaign.schedule_timezone, next_at.isoformat(),
        )
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', next_action_at=next_at,
        )
        return False

    account = _get_contact_account(cc)
    if not account or account.deleted_at:
        # Unlike quota exhaustion (a normal, recurring, self-resolving daily
        # condition — see below), "no valid sender account" only resolves if
        # someone fixes the campaign's account rotation. Bound the retry the
        # same way an SMTP send failure is bounded, so a permanently
        # misconfigured campaign eventually surfaces as failed instead of
        # polling forever every 15 minutes.
        attempts = cc.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
                status='failed', attempts=attempts,
                error='no valid connected sender account for this campaign',
                next_action_at=None,
            )
            SOCampaign.objects.filter(id=campaign.id).update(total_failed=F('total_failed') + 1)
            logger.error('so_drip: campaign %s contact %s failed permanently — no valid sender '
                        'account after %s attempts', campaign.id, cc.id, attempts)
        else:
            logger.error('so_drip: campaign %s has no valid sender account, contact %s attempt %s/%s',
                         campaign.id, cc.id, attempts, MAX_ATTEMPTS)
            SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
                status='active', attempts=attempts, next_action_at=now() + RETRY_DELAY,
            )
        return False

    if not _reserve_quota_slot(account):
        next_at = _next_utc_midnight()
        logger.info(
            'so_drip: campaign %s contact %s deferred — account %s (%s) at daily_limit=%s, retrying at %s',
            campaign.id, cc.id, account.id, account.email, account.daily_limit, next_at.isoformat(),
        )
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', next_action_at=next_at,
        )
        return False

    site_url = _site_url()
    try:
        from django.conf import settings
        enable_tracking = settings.ENABLE_EMAIL_TRACKING
        personalized_html, tracked_links = inject_tracking(
            variant.html_body, cc, site_url, enable_tracking=enable_tracking,
        )
        msg_id  = f'<{uuid.uuid4()}@{account.smtp_host}>'
        from_nm = campaign.from_name or account.display_name or account.email
        # The List-Unsubscribe header is a real link too — same rule as the
        # body: don't emit it pointing somewhere nothing is listening.
        unsub_url = f'{site_url}/so/unsubscribe/{cc.tracking_token}/' if enable_tracking else ''

        server = open_smtp(account)
        try:
            msg = build_message(from_nm, account.email, cc.email,
                                variant.subject, personalized_html, unsub_url, msg_id)
            refused = server.sendmail(account.email, cc.email, msg.as_bytes())
            if refused and cc.email in refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            try:
                server.quit()
            except Exception:
                pass

        if tracked_links:
            from Email_validate_app.models import SOTrackedLink
            SOTrackedLink.objects.bulk_create(tracked_links, ignore_conflicts=True)

    except Exception as exc:
        logger.warning('so_drip: send failed for contact %s (%s) step %s: %s',
                       cc.id, cc.email, cc.current_step, exc)
        _release_quota_slot(account)   # reservation must not survive a failed send
        _record_failure(cc, exc)
        return False

    _record_success(cc, campaign, step, variant, msg_id, account, personalized_html)
    return True


def _site_url():
    from django.conf import settings
    return getattr(settings, 'SITE_URL', 'https://waytoinbox.com').rstrip('/')


def _record_conversation_send(cc, campaign, account, subject, html, msg_id, sent_at):
    """Mirror a successful sequence send into the Inbox's conversation/message
    store. SOEvent (below) stays the analytics/counter log; this is the content
    a human actually reads in the Inbox. Lazily creates the SOConversation on
    the contact's first send, same self-heal shape as _get_contact_account."""
    from django.utils.html import strip_tags
    from Email_validate_app.services.so_inbox import cc_thread_key, upsert_conversation_message

    upsert_conversation_message(
        thread_key=cc_thread_key(cc.id), account=account, direction='outbound',
        is_sequence_step=True, subject=subject, body_html=html, body_text=strip_tags(html).strip(),
        from_email=account.email, to_email=cc.email, message_id=msg_id, sent_at=sent_at,
        campaign_contact=cc, campaign=campaign,
        prospect=(cc.prospect if cc.prospect_id else None),
        counterpart_email=cc.email,
    )


def _record_success(cc, campaign, step, variant, msg_id, account, personalized_html):
    from Email_validate_app.models import SOCampaignContact, SOEvent, SOCampaign

    sent_at = now()
    step_holder = cc.active_subsequence if cc.active_subsequence_id else campaign
    next_step = step_holder.steps.filter(order=cc.current_step + 1).first()

    fields = {
        'status': 'active', 'sent_at': sent_at, 'message_id': msg_id,
        'attempts': 0, 'current_step': cc.current_step + 1,
    }
    if next_step is None:
        fields['status']         = 'completed'
        fields['completed_at']   = sent_at
        fields['next_action_at'] = None
    else:
        fields['next_action_at'] = sent_at + timedelta(
            days=next_step.wait_days, hours=next_step.wait_hours,
        )

    SOCampaignContact.objects.filter(id=cc.id, status='sending').update(**fields)
    # account_id is kept for per-message traceability/reporting — not needed for
    # quota enforcement, which is done atomically via SOEmailAccountDailyUsage.
    SOEvent.objects.create(
        campaign_id=campaign.id, prospect_id=cc.prospect_id, email=cc.email,
        event_type='sent', metadata={'step': cc.current_step, 'account_id': account.id},
    )
    SOEvent.objects.create(
        campaign_id=campaign.id, prospect_id=cc.prospect_id, email=cc.email,
        event_type='delivered', metadata={'step': cc.current_step, 'account_id': account.id},
    )
    SOCampaign.objects.filter(id=campaign.id).update(
        total_sent=F('total_sent') + 1, total_delivered=F('total_delivered') + 1,
    )
    _record_conversation_send(cc, campaign, account, variant.subject, personalized_html, msg_id, sent_at)
    logger.info(
        'so_drip: campaign %s contact %s — step %s sent, %s',
        campaign.id, cc.id, cc.current_step,
        'sequence completed' if fields['status'] == 'completed'
        else f"next action at {fields['next_action_at'].isoformat()}",
    )


def _record_failure(cc, exc):
    from Email_validate_app.models import SOCampaignContact, SOCampaign

    attempts = cc.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='failed', attempts=attempts, error=str(exc)[:2000], next_action_at=None,
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_failed=F('total_failed') + 1)
        logger.error('so_drip: campaign contact %s (%s) failed permanently after %s attempts: %s',
                     cc.id, cc.email, attempts, exc)
    else:
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', attempts=attempts, error=str(exc)[:2000],
            next_action_at=now() + RETRY_DELAY,
        )
        logger.warning('so_drip: campaign contact %s (%s) attempt %s/%s failed, retrying at +%s',
                       cc.id, cc.email, attempts, MAX_ATTEMPTS, RETRY_DELAY)
