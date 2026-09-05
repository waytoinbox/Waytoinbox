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
for that wiring. Sales Outreach's real send volume (so_drip.py's
SOEmailAccountDailyUsage) is NOT narrowed during a trial -- an active trial
only limits TRIAL_LIMITS['sales_outreach'] below, how many SENDER ACCOUNTS
may be added, not how many emails per day an already-added one may send;
a per-day send cap override used to exist here (removed) and is not
reintroduced.

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
    'email_validation': 100,
    'email_marketing':  200,
    'sales_outreach':     2,
    'reputation':         2,
    'header_analysis':   25,
    'ip_blocklist':       5,
    'domain_blocklist':   5,
}
assert set(TRIAL_LIMITS) == set(SERVICE_KEYS), \
    "TRIAL_LIMITS must cover every SERVICE_KEYS entry"

SERVICE_LABELS = dict(SERVICE_CHOICES)


def is_trial_eligible(user):
    """Pure, no query -- `user` must be a loaded UserTable instance.
    True iff this account has never started a trial. Deliberately does NOT
    consider payment history -- that is a separate, broader rule (see
    can_offer_trial() below); this stays the narrow "never activated"
    check activate_trial() itself relies on for its own idempotency."""
    return user is not None and user.trial_started_at is None


def has_ever_paid(user_id):
    """True iff this user has ever completed a real payment -- any
    legacy Pay-As-You-Go or service-credit purchase (`Payment`, a row
    written only on verified payment -- see ServiceOrder's own docstring
    in models.py) or any subscription plan payment (`SubsPayment`, same:
    only ever created inside views/subscription.py's payment-verification
    branch, after Razorpay confirms the charge). Once true, the free
    trial offer is retired for this user permanently -- even if they
    never activated a trial at all.
    """
    from Email_validate_app.models import Payment, SubsPayment
    return (Payment.objects.filter(user_id=user_id).exists()
            or SubsPayment.objects.filter(user_id=user_id).exists())


def can_offer_trial(user):
    """Whether the trial button/popup should be shown, and whether
    activation should be allowed, at all: never started a trial AND never
    made a real payment. Broader than is_trial_eligible() alone -- a user
    who paid for something without ever trialing must not be offered the
    trial either, per the confirmed product rule ("activated the trial,
    OR made any payment" both retire the offer). `user` must be a loaded
    UserTable instance; this does one query (has_ever_paid) beyond what
    is_trial_eligible() alone needs.
    """
    return is_trial_eligible(user) and not has_ever_paid(user.id)


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
