"""
Warmup orchestration — the business-logic layer Celery tasks call into,
mirroring the role so_drip.py plays for campaign drip sending. Handles:

  - the ramp-up target computation (continuous, computed on demand)
  - daily quota reserve/release (WarmupDailyUsage, its own counter — kept
    separate from SOEmailAccountDailyUsage so warmup volume and real
    campaign volume are independent pools)
  - creating today's pending WarmupMessage rows with fair, least-recently-
    used receiver rotation and staggered send times
  - Start/Pause/Resume/Stop

Actual SMTP sending lives in warmup_sender.py; Gmail API verification lives
in warmup_receiver.py. This module never touches either directly.
"""

import random
import uuid
from datetime import timedelta, datetime, time as dt_time, timezone as dt_timezone

from django.db.models import F
from django.utils.timezone import now

DEFAULT_DAILY_TARGET      = 40
DEFAULT_RAMP_UP_DAYS      = 30
DEFAULT_RAMP_UP_INCREMENT = 2


def _next_utc_midnight():
    today_utc = now().date()
    return datetime.combine(today_utc + timedelta(days=1), dt_time.min, tzinfo=dt_timezone.utc)


def get_todays_target(warmup) -> int:
    """Continuous, computed fresh every call — never mutated by a scheduled
    task, so there's no midnight-rollover job and no drift risk. Ramps
    linearly by ramp_up_increment per day starting on day 1
    (days_elapsed=0 -> ramp_up_increment), capped at daily_target once
    reached — and stays capped there indefinitely afterward. Reaching the
    cap is not a stopping condition; it just means the target stops
    growing."""
    if not warmup.started_at:
        return 0
    days_elapsed = max(0, (now().date() - warmup.started_at.date()).days)
    ramped = warmup.ramp_up_increment * (days_elapsed + 1)
    return min(warmup.daily_target, ramped)


def reserve_quota_slot(account, warmup) -> bool:
    """Atomically claim one of `account`'s remaining warmup sends for today,
    gated by the computed ramp target — not SOEmailAccount.daily_limit
    (that's the campaign-sending limit, a separate pool). Same conditional-
    UPDATE claim pattern as so_drip.py::_reserve_quota_slot."""
    from Email_validate_app.models import WarmupDailyUsage

    today  = now().date()
    target = get_todays_target(warmup)
    WarmupDailyUsage.objects.get_or_create(account=account, date=today, defaults={'sent_count': 0})
    updated = WarmupDailyUsage.objects.filter(
        account=account, date=today, sent_count__lt=target,
    ).update(sent_count=F('sent_count') + 1)
    return bool(updated)


def release_quota_slot(account):
    """Give back a slot reserved by reserve_quota_slot — called only when the
    send attempt that reserved it then failed."""
    from Email_validate_app.models import WarmupDailyUsage

    today = now().date()
    WarmupDailyUsage.objects.filter(
        account=account, date=today, sent_count__gt=0,
    ).update(sent_count=F('sent_count') - 1)


def create_pending_messages_for_sender(warmup) -> int:
    """Top up today's pending queue for this sender up to the remaining
    ramp quota. Naturally idempotent — recomputes the remaining delta each
    call (counting existing non-cancelled rows created today), so re-running
    mid-tick creates zero extra rows once quota is met.

    Fair receiver rotation: receivers are drawn from ONE fixed, admin-managed
    pool shared across every sender for every user (WarmupReceiverAccount is
    no longer user-owned — see views/admin/warmup.py) and fetched ordered by
    last_assigned_at ascending (nulls first), so the least-recently-used
    receiver in the pool always goes first. If the pool is smaller than the
    volume needed this tick, the ordered list is cycled rather than repeatedly
    picking the same front-of-queue receiver, so distribution stays even
    even within a single tick.

    scheduled_for is staggered with jitter across the remaining hours of the
    UTC day, so a whole day's volume is never sent all at once.
    """
    from Email_validate_app.models import WarmupMessage, WarmupReceiverAccount

    account = warmup.account
    target  = get_todays_target(warmup)

    # Opportunistic display cache only — never read back as authoritative.
    if warmup.daily_current != target:
        warmup.daily_current = target
        warmup.save(update_fields=['daily_current'])

    if target <= 0:
        return 0

    today = now().date()
    existing_today = WarmupMessage.objects.filter(
        sender_account=account, created_at__date=today,
    ).exclude(status='cancelled').count()
    remaining = target - existing_today
    if remaining <= 0:
        return 0

    receivers = list(WarmupReceiverAccount.objects.filter(
        status='connected', deleted_at__isnull=True,
    ).order_by(F('last_assigned_at').asc(nulls_first=True)))
    if not receivers:
        return 0

    window_end     = _next_utc_midnight()
    window_seconds = max(1, int((window_end - now()).total_seconds()))

    created = 0
    for i in range(remaining):
        receiver = receivers[i % len(receivers)]
        scheduled_for = now() + timedelta(seconds=random.randint(0, window_seconds))
        WarmupMessage.objects.create(
            sender_account=account, sender_email=account.email,
            receiver_account=receiver, receiver_email=receiver.email,
            identifier=f'WTI-WARMUP-{uuid.uuid4().hex[:12]}',
            scheduled_for=scheduled_for,
        )
        WarmupReceiverAccount.objects.filter(id=receiver.id).update(last_assigned_at=now())
        created += 1
    return created


def start_warmup(account_ids, daily_target=None, ramp_up_days=None, ramp_up_increment=None):
    """Enroll (or re-activate) the given SOEmailAccounts in warmup.
    get_or_create means an account already enrolled before never gets a
    second/duplicate warmup config — re-running Start just reactivates it
    without resetting ramp progress."""
    from Email_validate_app.models import SOEmailAccount, SOEmailAccountWarmup

    results = []
    for account in SOEmailAccount.objects.filter(id__in=account_ids, deleted_at__isnull=True):
        warmup, created = SOEmailAccountWarmup.objects.get_or_create(
            account=account,
            defaults={
                'daily_target':      daily_target or DEFAULT_DAILY_TARGET,
                'ramp_up_days':      ramp_up_days or DEFAULT_RAMP_UP_DAYS,
                'ramp_up_increment': ramp_up_increment or DEFAULT_RAMP_UP_INCREMENT,
                'started_at':        now(),
                'status':            'active',
            },
        )
        if not created:
            update_fields = ['status']
            warmup.status = 'active'
            if not warmup.started_at:
                warmup.started_at = now()
                update_fields.append('started_at')
            warmup.save(update_fields=update_fields)
        results.append(warmup)
    return results


def pause_warmup(account_ids):
    from Email_validate_app.models import SOEmailAccountWarmup
    SOEmailAccountWarmup.objects.filter(account_id__in=account_ids, status='active').update(status='paused')


def resume_warmup(account_ids):
    """No message rows touched — existing pending rows just become eligible
    again on the next dispatch tick, and ramp progress (started_at) is
    untouched, so resume never creates duplicate volume for today."""
    from Email_validate_app.models import SOEmailAccountWarmup
    SOEmailAccountWarmup.objects.filter(account_id__in=account_ids, status='paused').update(status='active')


def stop_warmup(account_ids):
    """Deliberately only cancels 'pending' rows — an in-flight 'sending' row
    is left to finish naturally (the same accepted race-window behavior as
    the existing SOCampaignContact cancel action)."""
    from Email_validate_app.models import SOEmailAccountWarmup, WarmupMessage
    SOEmailAccountWarmup.objects.filter(account_id__in=account_ids).exclude(status='stopped').update(status='stopped')
    WarmupMessage.objects.filter(sender_account_id__in=account_ids, status='pending').update(
        status='cancelled', error='stopped by user',
    )
