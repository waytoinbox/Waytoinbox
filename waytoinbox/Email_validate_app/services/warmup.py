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

import logging
import math
import random
import uuid
from datetime import timedelta, datetime, time as dt_time, timezone as dt_timezone

from django.db.models import F
from django.utils.timezone import now

logger = logging.getLogger(__name__)

DEFAULT_DAILY_TARGET = 40
DEFAULT_RAMP_UP_DAYS = 30


def _next_utc_midnight():
    today_utc = now().date()
    return datetime.combine(today_utc + timedelta(days=1), dt_time.min, tzinfo=dt_timezone.utc)


# ── Shared WarmupMessage counting (V4.4 — moved here from
# views/warmup_dashboard.py, which now imports these instead of keeping its
# own copy, so the per-account Analytics Warmup tab can reuse the exact same
# counting logic instead of duplicating it) ─────────────────────────────────

def compute_message_counts(qs):
    """landing/read counts for any WarmupMessage queryset — used for both
    the /Warmup/ dashboard's today/total stat cards (queryset = all of a
    user's messages) and the per-account Analytics Warmup tab (queryset =
    one account's messages)."""
    return {
        'sent':      qs.filter(sent_at__isnull=False).count(),
        'inbox':     qs.filter(landing_location='inbox').count(),
        'spam':      qs.filter(landing_location='spam').count(),
        'rescued':   qs.filter(landing_location='spam', rescued_to_inbox=True).count(),
        'other':     qs.filter(landing_location='other').count(),
        'not_found': qs.filter(landing_location='not_found').count(),
        'read':      qs.filter(marked_read=True).count(),
    }


def pct(num, den):
    if not den:
        return None
    v = round(num / den * 100, 1)
    return int(v) if v == int(v) else v


def compute_account_warmup_analytics(account):
    """One account's warmup snapshot for the per-account Analytics Warmup
    tab: status, today's target/sent, landing breakdown (today + all-time),
    and placement percentages — the same metrics/terminology the /Warmup/
    dashboard already shows, just scoped to a single account's own
    WarmupMessage rows instead of every account the user owns.

    Returns None if this account was never enrolled in warmup at all (no
    SOEmailAccountWarmup row), so callers can render a clean "not started"
    state instead of a zeroed-out one."""
    from Email_validate_app.models import SOEmailAccountWarmup, WarmupMessage

    try:
        warmup = account.warmup
    except SOEmailAccountWarmup.DoesNotExist:
        return None

    all_msgs = WarmupMessage.objects.filter(sender_account=account)
    today_msgs = all_msgs.filter(created_at__date=now().date())
    today_stats = compute_message_counts(today_msgs)
    total_stats = compute_message_counts(all_msgs)
    checked_total = total_stats['inbox'] + total_stats['spam'] + total_stats['other']

    return {
        'status':         warmup.status,
        'status_display': warmup.get_status_display(),
        'daily_target':   warmup.daily_target,
        'daily_current':  warmup.daily_current,
        'todays_target':  get_todays_target(warmup),
        'today':          today_stats,
        'total':          total_stats,
        'failed':         all_msgs.filter(status='send_failed').count(),
        'placement_pct': {
            'inbox': pct(total_stats['inbox'], checked_total),
            'spam':  pct(total_stats['spam'], checked_total),
            'other': pct(total_stats['other'], checked_total),
        },
    }


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


def compute_ramp_increment(daily_target, ramp_up_days) -> int:
    """Single source of truth for ramp_up_increment — start_warmup() and
    update_warmup_settings() below must always call this instead of ever
    accepting a raw client-submitted increment, so the stored value can
    never drift from this formula.

    Ceiling division, floored at 1: guarantees the ramp (see
    get_todays_target() above, which multiplies this by the day count)
    reaches daily_target at or before day ramp_up_days, never later — and
    never stalls at a 0 increment regardless of how small daily_target is
    relative to ramp_up_days.
    """
    ramp_up_days = ramp_up_days or 1
    return max(1, math.ceil(daily_target / ramp_up_days))


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
        # Silent no-op otherwise — this is the dispatcher's only per-tick
        # entry point (warmup_dispatch_sends, every 5 minutes via Beat), so
        # logging here is naturally already rate-limited to once per active
        # warmup per tick, not a hot loop.
        logger.warning(
            'warmup: dispatcher skipped for account %s (%s) — no connected '
            'receiver accounts in the pool. Connect at least one via '
            '/wti-admin/warmup-receivers/ before warmup can send anything.',
            account.id, account.email,
        )
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


def start_warmup(account_ids, daily_target=None, ramp_up_days=None):
    """Enroll (or re-activate) the given SOEmailAccounts in warmup.
    get_or_create means an account already enrolled before never gets a
    second/duplicate warmup config — re-running Start just reactivates it
    without resetting ramp progress.

    ramp_up_increment is never accepted as a parameter — it's always
    derived from daily_target/ramp_up_days via compute_ramp_increment(),
    the single source of truth for that field (see its own docstring).

    Restart bug fix: previously, re-Starting an already-enrolled (e.g.
    stopped) account only ever updated status/started_at in the `if not
    created` branch below, silently discarding any new daily_target/
    ramp_up_days typed into the Start Warmup modal. Now, when either is
    explicitly passed, they're applied (and the increment recomputed) on
    reactivation too — mirroring what update_warmup_settings() already did
    for the Edit flow.
    """
    from Email_validate_app.models import SOEmailAccount, SOEmailAccountWarmup

    resolved_daily_target = daily_target or DEFAULT_DAILY_TARGET
    resolved_ramp_up_days = ramp_up_days or DEFAULT_RAMP_UP_DAYS
    resolved_increment = compute_ramp_increment(resolved_daily_target, resolved_ramp_up_days)

    results = []
    for account in SOEmailAccount.objects.filter(id__in=account_ids, deleted_at__isnull=True):
        warmup, created = SOEmailAccountWarmup.objects.get_or_create(
            account=account,
            defaults={
                'daily_target':      resolved_daily_target,
                'ramp_up_days':      resolved_ramp_up_days,
                'ramp_up_increment': resolved_increment,
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
            if daily_target is not None:
                warmup.daily_target = daily_target
                update_fields.append('daily_target')
            if ramp_up_days is not None:
                warmup.ramp_up_days = ramp_up_days
                update_fields.append('ramp_up_days')
            if daily_target is not None or ramp_up_days is not None:
                new_increment = compute_ramp_increment(warmup.daily_target, warmup.ramp_up_days)
                if new_increment != warmup.ramp_up_increment:
                    warmup.ramp_up_increment = new_increment
                    update_fields.append('ramp_up_increment')
            warmup.save(update_fields=update_fields)
        results.append(warmup)
    return results


def update_warmup_settings(account, daily_target=None, ramp_up_days=None):
    """The real update path start_warmup()'s get_or_create was missing for
    an ALREADY-enrolled account (V4.5): that function's `if not created`
    branch only ever touched `status`/`started_at`, so re-running Start
    with new ramp values silently discarded them (start_warmup() now also
    handles this on reactivation, see its own docstring — this function
    remains the Edit flow's own path, which must never touch status/
    started_at or create a row as a side effect of saving unrelated
    fields). Returns the updated SOEmailAccountWarmup, or None if this
    account was never enrolled.

    ramp_up_increment is never accepted as a parameter — always derived
    from daily_target/ramp_up_days via compute_ramp_increment() after
    applying whichever of the two were passed, so a partial update (e.g.
    only ramp_up_days changing) still recomputes using the account's
    current daily_target rather than a stale/default value.

    get_todays_target() reads daily_target/ramp_up_increment live off the
    model instance every call, so it automatically reflects these new
    values on its very next call — no second target calculation needed."""
    from Email_validate_app.models import SOEmailAccountWarmup

    try:
        warmup = account.warmup
    except SOEmailAccountWarmup.DoesNotExist:
        return None

    update_fields = []
    if daily_target is not None:
        warmup.daily_target = daily_target
        update_fields.append('daily_target')
    if ramp_up_days is not None:
        warmup.ramp_up_days = ramp_up_days
        update_fields.append('ramp_up_days')
    if daily_target is not None or ramp_up_days is not None:
        new_increment = compute_ramp_increment(warmup.daily_target, warmup.ramp_up_days)
        if new_increment != warmup.ramp_up_increment:
            warmup.ramp_up_increment = new_increment
            update_fields.append('ramp_up_increment')
    if update_fields:
        warmup.save(update_fields=update_fields)
    return warmup


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
