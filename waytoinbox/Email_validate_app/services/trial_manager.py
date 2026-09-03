"""7-Day Free Trial: eligibility, activation, and remaining-allowance
lookups. Mirrors credit_manager.py's structure and sits alongside it.

Activation is manual only -- activate_trial() is called exclusively from
views/credits.py::trial_activate(), never automatically (NOT from
verify_email(); verifying only makes a user ELIGIBLE to click "Activate
Free Trial", it does not start the clock).

Integration point: credit_manager.get_effective_balance() and
deduct_service_credits() call get_trial_remaining() from this module
internally, so every existing per-service view/deduction call site becomes
trial-aware automatically with no changes of its own. See credit_manager.py
for that wiring. so_drip.py separately calls
sales_outreach_daily_send_cap() for the per-sender-account daily send cap
during an active trial (a different limit from TRIAL_LIMITS['sales_outreach'],
which caps how many SENDER ACCOUNTS may be added, not how many emails per
day an already-added one may send).

Anti-abuse: "one trial per verified email, ever" relies entirely on
UserTable.user_email already being unique=True at the DB level (a duplicate
signup is rejected today, unaffected by this module) plus
trial_started_at being a permanent, one-way marker re-checked under a row
lock in activate_trial(). No device/phone/payment fingerprinting is added
-- by design, per the confirmed project scope.
"""
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from Email_validate_app.models import (
    SERVICE_CHOICES, SERVICE_KEYS, ServiceTrial, TrialUsageLog, UserTable,
)

TRIAL_DURATION_DAYS = 7

TRIAL_LIMITS = {
    'email_validation': 50,
    'email_marketing':  50,
    'sales_outreach':     1,
    'reputation':         1,
    'header_analysis':   10,
    'ip_blocklist':       1,
    'domain_blocklist':   1,
}
assert set(TRIAL_LIMITS) == set(SERVICE_KEYS), \
    "TRIAL_LIMITS must cover every SERVICE_KEYS entry"

# Sales Outreach only: while a user's trial is active, each of their sender
# accounts is additionally capped at this many emails/day (never higher than
# the account's own configured daily_limit -- see
# sales_outreach_daily_send_cap() and so_drip.py::_reserve_quota_slot).
# Independent of TRIAL_LIMITS['sales_outreach'] above, which caps how many
# sender accounts may be added during the trial, not how many emails per day
# an already-added one may send.
SALES_OUTREACH_TRIAL_DAILY_SEND_CAP = 7

SERVICE_LABELS = dict(SERVICE_CHOICES)


def is_trial_eligible(user):
    """Pure, no query -- `user` must be a loaded UserTable instance.
    True iff this account has never started a trial."""
    return user is not None and user.trial_started_at is None


def is_trial_active(user):
    """Pure, no query -- `user` must be a loaded UserTable instance with
    trial_started_at/trial_ends_at already on it. True iff a trial was
    started and its 7-day window hasn't elapsed yet."""
    return bool(user and user.trial_started_at and user.trial_ends_at
                and user.trial_ends_at > now())


def activate_trial(user):
    """Grant the one-time 7-day trial to `user`. Call ONLY from
    views/credits.py::trial_activate(), in response to the user explicitly
    clicking "Activate Free Trial" -- never automatically. That view is
    responsible for checking user.is_verified before calling this; this
    function itself does not check verification.

    Idempotent: if the user is already ineligible (checked once cheaply,
    then again under a row lock to close a double-submit race), this is a
    no-op that returns False and writes nothing. Never raises for the
    "already used" case -- only a real database error would raise.
    """
    if not is_trial_eligible(user):
        return False

    with transaction.atomic():
        locked = UserTable.objects.select_for_update().get(pk=user.pk)
        if locked.trial_started_at is not None:
            return False

        started_at = now()
        ends_at = started_at + timedelta(days=TRIAL_DURATION_DAYS)
        locked.trial_started_at = started_at
        locked.trial_ends_at = ends_at
        locked.save(update_fields=['trial_started_at', 'trial_ends_at'])

        ServiceTrial.objects.bulk_create([
            ServiceTrial(user=locked, service=svc, limit=TRIAL_LIMITS[svc], used=0)
            for svc in SERVICE_KEYS
        ])
        TrialUsageLog.objects.bulk_create([
            TrialUsageLog(
                user=locked, service=svc, entry_type='granted',
                amount=TRIAL_LIMITS[svc], balance_before=0,
                balance_after=TRIAL_LIMITS[svc], ref_type='trial_start',
                description=f"7-day trial granted: {SERVICE_LABELS[svc]}",
            )
            for svc in SERVICE_KEYS
        ])

    # Reflect the grant on the caller's already-loaded `user` object too,
    # in case it's used again in the same request after this call.
    user.trial_started_at = started_at
    user.trial_ends_at = ends_at
    return True


def get_trial_remaining(user_id, service):
    """Cheap, unlocked, read-only allowance remaining for `service`. 0 if
    the user never had a trial, the 7-day window has elapsed, or this
    service's allowance is already exhausted. This is the function
    credit_manager.py calls internally -- never call it as a spend check by
    itself; deduct_service_credits() re-checks under a row lock."""
    window = UserTable.objects.filter(pk=user_id).values(
        'trial_started_at', 'trial_ends_at').first()
    if not window or not window['trial_started_at']:
        return 0
    if not window['trial_ends_at'] or window['trial_ends_at'] <= now():
        return 0
    row = ServiceTrial.objects.filter(user_id=user_id, service=service).first()
    return max(0, row.limit - row.used) if row else 0


def sales_outreach_daily_send_cap(user_id):
    """Sales Outreach per-account daily send cap override while `user_id`'s
    trial is active, else None (no override -- caller keeps the account's
    own configured daily_limit unchanged).

    Cheap, unlocked, values()-only lookup -- same style as
    get_trial_remaining(). Takes a bare user_id rather than a loaded
    UserTable so callers can pass account.user_id (a FK id already present
    on any loaded SOEmailAccount row, no extra join/query) instead of having
    to select_related('account__user') just to call this.

    Reverting after trial expiry needs no separate code path: this is a
    live check against trial_ends_at on every call, exactly like
    get_trial_remaining() -- there is no stored/cached "trial active" flag
    anywhere in this system to go stale.
    """
    window = UserTable.objects.filter(pk=user_id).values(
        'trial_started_at', 'trial_ends_at').first()
    if not window or not window['trial_started_at']:
        return None
    if not window['trial_ends_at'] or window['trial_ends_at'] <= now():
        return None
    return SALES_OUTREACH_TRIAL_DAILY_SEND_CAP
