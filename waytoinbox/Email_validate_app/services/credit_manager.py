import secrets
import re
import logging
from datetime import datetime, timedelta

import pytz
from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils.timezone import now

from Email_validate_app.models import (
    CurrentCredits, TotalCredits, UsedCredits, CreditAuditLog, AllEmails, ListFiles,
    ServiceCredit, ServiceTrial, TrialUsageLog, ServiceCreditLot, UserTable,
    SERVICE_CHOICES, SERVICE_KEYS,
)

logger = logging.getLogger(__name__)

# Phase 3 (expiring-credit-lot spending): exact duration, never a
# calendar-day rule -- a lot paid at 2026-09-11 10:30:00 UTC expires at
# exactly 2026-10-11 10:30:00 UTC, independent of timezone or DST.
LOT_LIFETIME = timedelta(hours=720)

# Product decision: Email Validation lots still carry the same purchased_at/
# expires_at metadata as every other service (for audit/history), but their
# remaining credit must never actually expire -- only these services' lots
# are excluded from the expires_at__gt=now() spend gate below and from
# tasks/credit_expiry.py's finalization sweep. Every other service's lot
# expiry behavior is completely unchanged.
NON_EXPIRING_LOT_SERVICES = {'email_validation'}

# Old-credit retirement (business decision): ServiceCredit.balance (the
# "wallet") and CurrentCredits (the legacy vc/ac/cc pools) are no longer
# usable, for spending OR for what's displayed as a usable balance.
# Existing rows in both are deliberately left untouched -- nothing is
# migrated, copied, zeroed, or deleted -- they simply stop being counted.
# The old code paths below are kept structurally intact (not deleted) and
# gated behind this single flag so the change is reversible and so spend
# (deduct_service_credits) and display (get_effective_balance/
# get_all_service_balances) can never drift out of sync with each other.
LEGACY_BALANCES_SPENDABLE = False


def generate_receipt_id(timezone='Asia/Kolkata'):
    random_part = secrets.token_hex(5)[:6]
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    user_timezone = pytz.timezone(timezone)
    current_datetime = now_utc.astimezone(user_timezone).replace(tzinfo=None)
    date_part = re.sub(r'[^a-zA-Z0-9]', '', str(current_datetime))
    return f"INC{random_part}{date_part}"


# ── Getters ───────────────────────────────────────────────────────────────────

def get_vc_current_credit(user_id):
    try:
        credit = CurrentCredits.objects.get(user_id=user_id)
        return credit.vc_current_credits or 0
    except CurrentCredits.DoesNotExist:
        return 0


def get_ac_current_credit(user_id):
    try:
        credit = CurrentCredits.objects.get(user_id=user_id)
        return credit.ac_current_credits or 0
    except CurrentCredits.DoesNotExist:
        return 0


def get_cc_current_credit(user_id):
    try:
        credit = CurrentCredits.objects.get(user_id=user_id)
        return credit.cc_current_credits or 0
    except CurrentCredits.DoesNotExist:
        return 0


# Backward-compat aliases — keep until all callers are updated
def get_current_credit(user_id):
    return get_vc_current_credit(user_id)


def get_ip_current_credit(user_id):
    return get_ac_current_credit(user_id)


# ── Credit inserters ──────────────────────────────────────────────────────────

def insert_vc_credits(request, user_id, amount, ref_type='payg', ref_id=''):
    """Add Validation Credits (VC) to a user's balance with audit log."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    amount = int(amount) if amount else 0
    if amount <= 0:
        return

    # DB-04: hold a row lock for the entire read-modify-write-log sequence so the
    # audit log balance_before/after always matches the actual balance update.
    with transaction.atomic():
        obj, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        balance_before = obj.vc_current_credits or 0
        obj.vc_total_credits   = (obj.vc_total_credits or 0) + amount
        obj.vc_current_credits = balance_before + amount
        obj.save(update_fields=['vc_total_credits', 'vc_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='vc',
            entry_type='credit',
            amount=amount,
            balance_before=balance_before,
            balance_after=obj.vc_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=f"Added {amount} Validation Credits",
        )
        TotalCredits.objects.create(user_id=user_id, vc_credits=amount, vc_buying_date=now_utc)


def insert_ac_credits(request, user_id, amount, ref_type='', ref_id=''):
    """Add Analysis Credits (AC) to a user's balance with audit log."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    amount = int(amount) if amount else 0
    if amount <= 0:
        return

    with transaction.atomic():
        obj, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        balance_before = obj.ac_current_credits or 0
        obj.ac_total_credits   = (obj.ac_total_credits or 0) + amount
        obj.ac_current_credits = balance_before + amount
        obj.save(update_fields=['ac_total_credits', 'ac_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='ac',
            entry_type='credit',
            amount=amount,
            balance_before=balance_before,
            balance_after=obj.ac_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=f"Added {amount} Analysis Credits",
        )
        TotalCredits.objects.create(user_id=user_id, ac_credits=amount, ac_buying_date=now_utc)


def insert_cc_credits(request, user_id, amount, ref_type='', ref_id=''):
    """Add Contact Credits (CC) to a user's balance with audit log."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    amount = int(amount) if amount else 0
    if amount <= 0:
        return

    with transaction.atomic():
        obj, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        balance_before = obj.cc_current_credits or 0
        obj.cc_total_credits   = (obj.cc_total_credits or 0) + amount
        obj.cc_current_credits = balance_before + amount
        obj.save(update_fields=['cc_total_credits', 'cc_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='cc',
            entry_type='credit',
            amount=amount,
            balance_before=balance_before,
            balance_after=obj.cc_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=f"Added {amount} Contact Credits",
        )
        TotalCredits.objects.create(user_id=user_id, cc_credits=amount, cc_buying_date=now_utc)


# Backward-compat aliases
def insert_credits(request, user_id, credit):
    return insert_vc_credits(request, user_id, int(credit) if credit else 0, ref_type='payg')


# ── Credit deductors (atomic, select_for_update) ──────────────────────────────

def deduct_vc_credits(user_id, count, ref_type='validation', ref_id='', description=''):
    """Deduct Validation Credits atomically. Raises ValueError if insufficient."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    with transaction.atomic():
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        if (obj.vc_current_credits or 0) < count:
            raise ValueError(
                f"Insufficient VC credits: need {count}, have {obj.vc_current_credits or 0}"
            )
        balance_before = obj.vc_current_credits or 0
        obj.vc_used_credits    = (obj.vc_used_credits or 0) + count
        obj.vc_current_credits = balance_before - count
        obj.save(update_fields=['vc_used_credits', 'vc_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='vc',
            entry_type='debit',
            amount=-count,
            balance_before=balance_before,
            balance_after=obj.vc_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=description or f"Used {count} Validation Credits",
        )
        UsedCredits.objects.create(user_id=user_id, vc_used_credits=count, vc_used_date=now_utc)


def deduct_ac_credits(user_id, count, ref_type='ip_check', ref_id='', description=''):
    """Deduct Analysis Credits atomically. Raises ValueError if insufficient."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    with transaction.atomic():
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        if (obj.ac_current_credits or 0) < count:
            raise ValueError(
                f"Insufficient AC credits: need {count}, have {obj.ac_current_credits or 0}"
            )
        balance_before = obj.ac_current_credits or 0
        obj.ac_used_credits    = (obj.ac_used_credits or 0) + count
        obj.ac_current_credits = balance_before - count
        obj.save(update_fields=['ac_used_credits', 'ac_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='ac',
            entry_type='debit',
            amount=-count,
            balance_before=balance_before,
            balance_after=obj.ac_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=description or f"Used {count} Analysis Credits",
        )
        UsedCredits.objects.create(user_id=user_id, ac_used_credits=count, ac_used_date=now_utc)


def deduct_cc_credits(user_id, count, ref_type='campaign', ref_id='', description=''):
    """Deduct Contact Credits atomically. Raises ValueError if insufficient."""
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    with transaction.atomic():
        obj = CurrentCredits.objects.select_for_update().get(user_id=user_id)
        if (obj.cc_current_credits or 0) < count:
            raise ValueError(
                f"Insufficient CC credits: need {count}, have {obj.cc_current_credits or 0}"
            )
        balance_before = obj.cc_current_credits or 0
        obj.cc_used_credits    = (obj.cc_used_credits or 0) + count
        obj.cc_current_credits = balance_before - count
        obj.save(update_fields=['cc_used_credits', 'cc_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='cc',
            entry_type='debit',
            amount=-count,
            balance_before=balance_before,
            balance_after=obj.cc_current_credits,
            ref_type=ref_type,
            ref_id=str(ref_id),
            description=description or f"Used {count} Contact Credits",
        )
        UsedCredits.objects.create(user_id=user_id, cc_used_credits=count, cc_used_date=now_utc)


def expire_subscription_credits(user_id, sub):
    """Record a subscription expiry WITHOUT clearing any credit balance.

    Credits no longer expire. This function used to zero
    CurrentCredits.ac/cc_current_credits when a plan lapsed; it deliberately
    no longer does, so a customer who lets a plan expire keeps everything they
    paid for. ServiceCredit balances were never touched here in the first
    place.

    It is kept (rather than deleted along with its call in
    subscription_expiry_job) so the expiry path stays explicit and auditable:
    the job still flips SubsPayment.plan_status to Inactive, notifies the user
    and emails them, and this records what was retained.

    No balance row is read for update, no row is created, and nothing is
    written to CreditAuditLog — the ledger records changes, and by design
    nothing changes here.

    Returns the retained balances, for the caller's log line.
    """
    from Email_validate_app.models import CurrentCredits

    plan_name = sub.subs_plan or 'Subscription'
    obj = CurrentCredits.objects.filter(user_id=user_id).first()
    retained = {
        'ac': (obj.ac_current_credits or 0) if obj else 0,
        'cc': (obj.cc_current_credits or 0) if obj else 0,
        'vc': (obj.vc_current_credits or 0) if obj else 0,
    }
    logger.info(
        "Subscription expiry (user %s, %s): credits RETAINED "
        "(ac=%s, cc=%s, vc=%s) - balances no longer expire.",
        user_id, plan_name, retained['ac'], retained['cc'], retained['vc'],
    )
    return retained


# ── Pricing and bulk-download utilities ───────────────────────────────────────

_PLANS = [
    (5000, 0.007, "Plan 1"),
    (50000, 0.004, "Plan 2"),
    (100000, 0.003, "Plan 3"),
    (500000, 0.002, "Plan 4"),
    (1000000, 0.0024, "Plan 5"),
    (2000000, 0.001, "Plan 6"),
]


def calculate_price(credits):
    for threshold, rate, plan_name in _PLANS:
        if credits <= threshold:
            return True, (credits * rate, rate)
    return False, "Interested in Buying Over 2 Million Credits? Contact Us!"


_TABLE_NAME_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')


def manage_credits(selected_option, table_name, user_id, timezone_str):
    # DB-06: replace weak isidentifier() check with strict pattern allowlist
    if not _TABLE_NAME_RE.fullmatch(table_name or ''):
        logger.error("manage_credits: invalid table name %r", table_name)
        return "Invalid table name or validation error"

    try:
        # DB-06: scope lookup to the requesting user to prevent IDOR
        file_entry = ListFiles.objects.get(table_name=table_name, user_id=user_id)
    except ListFiles.DoesNotExist:
        logger.error("manage_credits: no record for table=%r user=%s", table_name, user_id)
        return "File entry not found in ListFiles."

    def _fetch_rows(file_entry, selected_option):
        qs = AllEmails.objects.filter(file_id=file_entry.file_id)
        if selected_option in ['valid', 'invalid']:
            qs = qs.filter(validation_results=selected_option.capitalize())
        rows = []
        for r in qs.order_by("id"):
            extra = r.extra_data or {}
            row = {"Win_Id": extra.get("Win_Id", "")}
            _internal = {"Win_Id", "reason", "validation_result", "result_reason"}
            for k, v in extra.items():
                if k not in _internal:
                    row[k] = v
            if r.email not in row.values():
                row["email"] = r.email
            row["validation_results"] = r.validation_results or ""
            rows.append(row)
        return rows

    if file_entry.credite_status == "Credited":
        return _fetch_rows(file_entry, selected_option)

    row_count = AllEmails.objects.filter(
        file_id=file_entry.file_id,
        validation_results__in=["Valid", "Invalid"],
    ).count()

    # Phase 6 commit 9: the balance is the email_validation service wallet plus
    # the legacy VC pool behind it, rather than the raw VC column. Without this
    # a customer whose credits live entirely in the new wallet could not
    # download results they had already paid to validate.
    current_credit = get_effective_balance(user_id, 'email_validation')
    if row_count > current_credit:
        logger.warning(f"Insufficient credits: {current_credit} available, {row_count} required.")
        return str(row_count)

    # Phase 3: the charge is now a shared, ListFiles-row-locked helper
    # (charge_ev_bulk_file, defined below) also used by verify_emails()'s
    # own start-time charge, so whichever of the two actually fires first
    # for THIS file is the only one that ever deducts anything.
    try:
        charge_ev_bulk_file(
            file_entry, row_count, ref_type='validation',
            description=f"Bulk download: {table_name}",
        )
    except InsufficientCredits:
        # Lost a race against another spend since the check above. Report it the
        # same way the check does, so download_results() routes into its
        # existing need_credits flow instead of raising into a 500.
        logger.warning("Bulk download charge lost a race for user %s on %s",
                       user_id, table_name)
        return str(row_count)

    return _fetch_rows(file_entry, selected_option)


# ══════════════════════════════════════════════════════════════════════════════
# Service-based credit system
#
# Everything above this line is the legacy VC/AC/CC system and is left exactly
# as it was — the old subscription/PAYG flows still call it.
#
# The new system stores balances in ServiceCredit (one row per user+service).
# Legacy balances are NEVER migrated, copied or zeroed. Instead each service
# maps to the legacy pool it used to draw from, and a spend that exceeds the
# new wallet falls back to that pool for the remainder. This preserves the
# shared-AC semantics exactly: one AC pool of 100 stays a single pool of 100
# usable across four services, rather than becoming 4 x 100.
# ══════════════════════════════════════════════════════════════════════════════

SERVICE_LABELS = dict(SERVICE_CHOICES)

# service -> legacy CurrentCredits column prefix it historically spent from.
# The four analysis services deliberately share 'ac'.
SERVICE_LEGACY_POOL = {
    'email_validation': 'vc',
    'email_marketing':  'cc',
    'reputation':       'ac',
    'header_analysis':  'ac',
    'ip_blocklist':     'ac',
    'domain_blocklist': 'ac',
    'sales_outreach':   None,   # new service — never had a legacy pool
}


class InsufficientCredits(ValueError):
    """Not enough credits across the trial allowance, the new wallet, AND
    the legacy pool.

    Subclasses ValueError deliberately: every existing deduction call site
    already handles ValueError (that is what deduct_vc/ac/cc_credits raise),
    so swapping them onto the new API cannot silently break their error paths.

    trial_active/trial_exhausted are additive, optional attributes (every
    existing raise site outside deduct_service_credits, e.g.
    ensure_service_credits, keeps constructing this with just
    service/needed/available) -- they exist so a caller COULD show a more
    specific message ("your trial ran out" vs "you have no credits"), but no
    existing call site's message/response shape is changed to use them.
    """

    def __init__(self, service, needed, available, trial_active=False,
                 trial_exhausted=False):
        self.service   = service
        self.needed    = needed
        self.available = available
        self.trial_active    = trial_active
        self.trial_exhausted = trial_exhausted
        super().__init__(
            f"Insufficient {SERVICE_LABELS.get(service, service)} credits: "
            f"need {needed}, have {available}"
        )


def _legacy_balance(user_id, service):
    """Spendable legacy balance for `service`, or 0 if it has no legacy pool."""
    pool = SERVICE_LEGACY_POOL.get(service)
    if not pool:
        return 0
    try:
        cc = CurrentCredits.objects.get(user_id=user_id)
    except CurrentCredits.DoesNotExist:
        return 0
    return getattr(cc, f'{pool}_current_credits', 0) or 0


def get_service_balance(user_id, service):
    """Balance in the NEW wallet only (excludes legacy).

    Retired as a spendable/effective source (see LEGACY_BALANCES_SPENDABLE)
    -- kept as-is for whatever still calls it directly (e.g. admin
    reporting), but get_effective_balance()/get_all_service_balances() no
    longer add this in while the flag is False."""
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    row = ServiceCredit.objects.filter(user_id=user_id, service=service).first()
    return row.balance if row else 0


def get_lot_balance(user_id, service):
    """Usable remaining credit from ServiceCreditLot ONLY -- the new
    system's actual spendable-balance source. Mirrors EXACTLY the
    eligibility rule deduct_service_credits() uses to pick its FEFO
    candidate lots (status=active, quantity_remaining>0, and expires_at in
    the future unless `service` is in NON_EXPIRING_LOT_SERVICES), so this
    can never show a number deduct_service_credits() can't actually honor.
    Revoked/expired lots (status != active) are excluded by the status
    filter alone, same as the deduction path."""
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    qs = ServiceCreditLot.objects.filter(
        user_id=user_id, service=service,
        status=ServiceCreditLot.STATUS_ACTIVE, quantity_remaining__gt=0,
    )
    if service not in NON_EXPIRING_LOT_SERVICES:
        qs = qs.filter(expires_at__gt=now())
    return qs.aggregate(total=Sum('quantity_remaining'))['total'] or 0


def get_effective_balance(user_id, service):
    """What the user can actually spend right now: trial allowance (if
    active) + usable ServiceCreditLot remaining.

    Old-credit retirement: ServiceCredit.balance and the legacy CurrentCredits
    pool are NO LONGER counted here while LEGACY_BALANCES_SPENDABLE is False
    -- a user whose only credit sits in either of those old sources sees
    (and can spend) exactly 0 until they hold a live trial allowance or make
    a new purchase. This is deliberately the SAME flag deduct_service_credits()
    checks, so what's displayed can never promise more than what a spend can
    actually honor. Existing old balances are never migrated, copied, or
    modified by this — only what's COUNTED changes.

    This is the number to gate actions on and to show next to a service. Note
    that for the four analysis services this used to double-count a SHARED
    legacy pool if summed across them — see get_all_service_balances(), which
    still reports that shared pool separately (now uninvolved in 'effective').

    Trial is counted first because deduct_service_credits() spends it
    first (it's free and time-boxed) — if this function didn't also count
    it first, a call site could see "you have enough" here and then have
    deduct_service_credits() draw from a different total.
    """
    from Email_validate_app.services.trial_manager import get_trial_remaining
    total = get_trial_remaining(user_id, service) + get_lot_balance(user_id, service)
    if LEGACY_BALANCES_SPENDABLE:
        total += get_service_balance(user_id, service) + _legacy_balance(user_id, service)
    return total


def get_all_service_balances(user_id):
    """All seven balances in ONE query (plus one for the legacy row, plus
    one for the trial window/rows).

    Used by the context processor on every authenticated request, so it must
    stay cheap and must never create rows.

    Returns:
        {
          'services': {service: {'new': int, 'legacy': int, 'trial': int,
                                  'effective': int}},
          'legacy_shared': {'ac': int, 'vc': int, 'cc': int},
          'trial_active': bool,
          'trial_ends_at': datetime | None,
        }

    `legacy_shared['ac']` is ONE pool backing four services. The UI must show
    it as a single shared figure, never as four independent balances, or a
    user with 100 AC appears to have 400.

    'effective' is trial-inclusive (trial + new + legacy). The two purchase
    pages (views/subscription.py, views/billing.py::pricing) deliberately
    read only ['new'] already (see their own comments on legacy
    double-counting) and are unaffected by this; context_processors.py and
    views/profile.py read ['effective'] for display and are the two places
    meant to pick up trial figures.

    Old-credit retirement: while LEGACY_BALANCES_SPENDABLE is False, 'new'
    reports the usable ServiceCreditLot balance (get_lot_balance()) instead
    of ServiceCredit.balance, and per-service 'legacy' reports 0 instead of
    the CurrentCredits pool -- so 'effective' (= new + legacy + trial) stays
    exactly consistent with get_effective_balance() and with what
    deduct_service_credits() can actually spend. 'legacy_shared' below is
    unaffected -- it's a separate, clearly-labelled historical figure, never
    folded into 'new'/'effective'.
    """
    new_balances = dict(
        ServiceCredit.objects.filter(user_id=user_id).values_list('service', 'balance')
    )
    cc = CurrentCredits.objects.filter(user_id=user_id).first()
    legacy = {
        'vc': (cc.vc_current_credits or 0) if cc else 0,
        'ac': (cc.ac_current_credits or 0) if cc else 0,
        'cc': (cc.cc_current_credits or 0) if cc else 0,
    }

    window = UserTable.objects.filter(pk=user_id).values(
        'trial_started_at', 'trial_ends_at').first()
    trial_active = bool(window and window['trial_started_at']
                        and window['trial_ends_at']
                        and window['trial_ends_at'] > now())
    trial_rows_by_service = {}
    if trial_active:
        trial_rows_by_service = {
            row['service']: row
            for row in ServiceTrial.objects.filter(user_id=user_id)
                                            .values('service', 'used', 'limit')
        }

    services = {}
    for service in SERVICE_KEYS:
        pool = SERVICE_LEGACY_POOL.get(service)
        trial_row = trial_rows_by_service.get(service)
        trial_rem = max(0, trial_row['limit'] - trial_row['used']) if trial_row else 0
        if LEGACY_BALANCES_SPENDABLE:
            new = new_balances.get(service, 0)
            leg = legacy.get(pool, 0) if pool else 0
        else:
            new = get_lot_balance(user_id, service)
            leg = 0
        services[service] = {
            'new': new, 'legacy': leg, 'trial': trial_rem,
            'effective': trial_rem + new + leg,
        }

    return {
        'services': services,
        'legacy_shared': legacy,
        'trial_active': trial_active,
        'trial_ends_at': window['trial_ends_at'] if window else None,
    }


def add_service_credits(user_id, service, amount, ref_type='service_purchase',
                        ref_id='', description=''):
    """Grant credits directly to the permanent wallet. Never expires.

    Phase 3: no longer called by either live purchase flow (service
    checkout, EV PAYG) — both now call grant_credit_lot() below instead, so
    a newly successful purchase can never inflate this balance again (it
    stays exactly what it was before Phase 3, forever). Kept for admin
    adjustments and any other grandfathered-wallet use; not deleted.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    amount = int(amount or 0)
    if amount <= 0:
        return

    with transaction.atomic():
        ServiceCredit.objects.get_or_create(user_id=user_id, service=service)
        row = ServiceCredit.objects.select_for_update().get(
            user_id=user_id, service=service)

        before = row.balance
        row.balance         = before + amount
        row.total_purchased = (row.total_purchased or 0) + amount
        row.save(update_fields=['balance', 'total_purchased', 'updated_at'])

        CreditAuditLog.objects.create(
            user_id=user_id, credit_type=service, entry_type='credit',
            amount=amount, balance_before=before, balance_after=row.balance,
            ref_type=ref_type, ref_id=str(ref_id),
            description=description or f"Added {amount} {SERVICE_LABELS[service]} credits",
        )


def grant_credit_lot(user_id, service, amount, *, source, purchased_at,
                     payment=None, order=None, ref_type='service_purchase',
                     ref_id='', description=''):
    """Grant NEWLY PURCHASED credit as an expiring lot — the Phase 3 grant
    path for both live purchase flows (service checkout, EV PAYG). Never
    touches ServiceCredit.balance or CurrentCredits: those are read by
    deduct_service_credits() as before, but nothing in this function writes
    to either, so no new purchase can ever change what they already were.

    `purchased_at` must be the single timestamp the caller already captured
    once at payment verification (e.g. the same value written to
    ServiceOrder.paid_at) — never computed fresh in here — so sibling lots
    created for several services in one multi-service cart share the exact
    same purchased_at/expires_at pair, with no drift between them.

    expires_at = purchased_at + LOT_LIFETIME (exactly 30*24h, never a
    calendar-day rule). `payment`/`order` are the FKs the caller already
    holds locked in its own outer transaction; ServiceCreditLot's own
    UniqueConstraint(payment, service) is the exactly-once guard for a
    replayed verification — this function does not need its own duplicate
    check, matching how add_service_credits()/Payment.objects.create()
    already rely on a similar constraint plus the caller's IntegrityError
    handling.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    amount = int(amount or 0)
    if amount <= 0:
        return None

    expires_at = purchased_at + LOT_LIFETIME

    with transaction.atomic():
        lot = ServiceCreditLot.objects.create(
            user_id=user_id, service=service,
            payment=payment, order=order, source=source,
            quantity_purchased=amount, quantity_remaining=amount,
            quantity_used=0, quantity_expired=0, quantity_revoked=0,
            status=ServiceCreditLot.STATUS_ACTIVE,
            purchased_at=purchased_at, expires_at=expires_at,
        )
        CreditAuditLog.objects.create(
            user_id=user_id, credit_type=service, entry_type='credit',
            amount=amount, balance_before=0, balance_after=amount,
            ref_type=ref_type, ref_id=str(ref_id), lot=lot, service=service,
            description=description or
                f"Purchased {amount} {SERVICE_LABELS[service]} credits "
                f"(expires {expires_at:%Y-%m-%d %H:%M} UTC)",
        )
    return lot


def ensure_service_credits(user_id, service, count):
    """Read-only preflight for bulk work: raise InsufficientCredits if the
    user cannot cover `count` right now.

    This is a check, not a reservation — deduct_service_credits re-checks under
    a row lock, so the actual spend is still safe. Its purpose is to fail a
    750-email bulk job BEFORE any work starts, rather than part-way through.
    """
    count = int(count or 0)
    if count <= 0:
        return
    available = get_effective_balance(user_id, service)
    if available < count:
        raise InsufficientCredits(service, count, available)


def deduct_service_credits(user_id, service, count, ref_type='', ref_id='',
                           description='', spend_id=None):
    """Spend `count` credits for `service`: trial allowance first (if
    active), then active ServiceCreditLot rows (FEFO — earliest expires_at
    first), then the permanent wallet, then the legacy pool for any
    remainder. Lots are also required to be unexpired (expires_at in the
    future) EXCEPT for NON_EXPIRING_LOT_SERVICES (email_validation), whose
    remaining lot credit stays spendable past its nominal expires_at.

    Worked example from the spec:
        new email_validation = 20, legacy vc = 100, request 50
        -> 20 from new, 30 from legacy
        -> new = 0, legacy = 70
    (trial and lots are spent first, ahead of both permanent sources — see
    below. For a user with no lots yet — every existing customer as of
    Phase 3 — the lot step below finds nothing and this is byte-identical
    to the pre-Phase-3 behavior above.)

    Atomicity: the whole read-modify-write is inside one transaction with
    select_for_update() on every row it touches, so concurrent spends cannot
    overdraw. Critically, the four analysis services lock the SAME
    CurrentCredits row, so two of them racing on a shared AC pool serialise
    correctly and cannot both spend the same credit.

    Lock order is fixed at ServiceCredit -> ServiceTrial -> ServiceCreditLot
    -> CurrentCredits everywhere in this module. Any future code touching
    more than one of these MUST use the same order or it can deadlock
    against this. ServiceCredit stays locked FIRST specifically because
    reputation.py, so_email_accounts.py, and blocklist.py all pre-lock
    ServiceCredit themselves before calling this function (their own
    comments say so, to serialise concurrent adds) — ServiceTrial's lock has
    to slot in after that external lock, never before it, or those call
    sites could deadlock against a path that only ever calls this function
    directly. ServiceCreditLot rows are locked third: unlike ServiceCredit,
    a lot is never locked before it exists (it is only ever queried among
    rows a prior, already-committed purchase created), so the "missing row"
    hazard that makes ServiceCredit need get_or_create-before-lock does not
    apply here. Spend ORDER (trial -> lots -> new -> legacy) is a
    business-logic decision and is independent of lock ACQUISITION order —
    the two don't need to match.

    `spend_id` groups every CreditAuditLog/TrialUsageLog row this one call
    writes, so a spend split across several sources (e.g. two lots plus the
    wallet) can be reconstructed and — for the caller cases that support it
    — refunded via refund_service_credits(..., spend_id=...). If omitted, a
    fresh one is generated; existing callers that never mention it are
    unaffected other than gaining this grouping key. Returns the spend_id
    actually used (None if count<=0, since nothing was spent).

    Raises InsufficientCredits (a ValueError) without writing anything if the
    combined balance cannot cover the request — never partially deducts.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    count = int(count or 0)
    if count <= 0:
        return None

    spend_id = spend_id or secrets.token_hex(18)
    pool = SERVICE_LEGACY_POOL.get(service)
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

    with transaction.atomic():
        # 1. ServiceCredit — lock position UNCHANGED (see docstring: external
        #    call sites depend on this being first).
        ServiceCredit.objects.get_or_create(user_id=user_id, service=service)
        row = ServiceCredit.objects.select_for_update().get(
            user_id=user_id, service=service)

        # 2. ServiceTrial — locked second. No get_or_create: a missing
        #    row means "this user never had a trial" (the common case), and
        #    that must stay a single cheap SELECT, not a row-creating write.
        window = UserTable.objects.filter(pk=user_id).values(
            'trial_started_at', 'trial_ends_at').first()
        trial_active = bool(window and window['trial_started_at']
                            and window['trial_ends_at']
                            and window['trial_ends_at'] > now())
        trial_row = None
        from_trial = 0
        if trial_active:
            trial_row = ServiceTrial.objects.select_for_update().filter(
                user_id=user_id, service=service).first()
            if trial_row:
                trial_remaining = trial_row.limit - trial_row.used
                from_trial = min(trial_remaining, count)

        remainder = count - from_trial

        # 3. ServiceCreditLot — NEW, locked third. FEFO: earliest expires_at
        #    first, purchased_at/id as deterministic tie-breakers. Only
        #    queried when there's still something to cover, so a user with
        #    no purchases since Phase 3 never touches this table at all.
        #
        #    Email Validation lots are excluded from the expires_at__gt=now()
        #    gate (NON_EXPIRING_LOT_SERVICES) -- their remaining credit stays
        #    spendable past the nominal expiry date; FEFO ordering itself is
        #    unaffected, still earliest-expires_at-first among whatever
        #    remains active.
        lots_consumed = []  # [(lot, n), ...]
        if remainder:
            candidate_lots = ServiceCreditLot.objects.select_for_update().filter(
                user_id=user_id, service=service,
                status=ServiceCreditLot.STATUS_ACTIVE, quantity_remaining__gt=0,
            )
            if service not in NON_EXPIRING_LOT_SERVICES:
                candidate_lots = candidate_lots.filter(expires_at__gt=now())
            candidate_lots = candidate_lots.order_by('expires_at', 'purchased_at', 'id')
            for lot in candidate_lots:
                if remainder <= 0:
                    break
                n = min(lot.quantity_remaining, remainder)
                if n > 0:
                    lots_consumed.append((lot, n))
                    remainder -= n
        from_lots = sum(n for _, n in lots_consumed)

        # 4. Wallet (ServiceCredit.balance) — same row already locked in step 1.
        #    Old-credit retirement: unreachable while LEGACY_BALANCES_SPENDABLE
        #    is False. The code is kept structurally intact (not deleted) for
        #    a possible later cleanup phase, but no new deduction can draw
        #    from this old balance until the flag is flipped back.
        from_new = 0
        if LEGACY_BALANCES_SPENDABLE:
            from_new = min(row.balance, remainder) if remainder else 0
            remainder -= from_new

        # 5. CurrentCredits (legacy) — lock position UNCHANGED, still last.
        #    Old-credit retirement: same gating as step 4 above — unreachable
        #    while LEGACY_BALANCES_SPENDABLE is False.
        cc = None
        from_legacy = 0
        if LEGACY_BALANCES_SPENDABLE and remainder and pool:
            cc, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
            cc = CurrentCredits.objects.select_for_update().get(user_id=user_id)
            legacy_avail = getattr(cc, f'{pool}_current_credits', 0) or 0
            from_legacy = min(legacy_avail, remainder)
            remainder -= from_legacy

        if remainder > 0:
            # Nothing has been written yet — the transaction simply unwinds.
            raise InsufficientCredits(
                service, count, from_trial + from_lots + from_new + from_legacy,
                trial_active=trial_active,
                trial_exhausted=bool(trial_active and trial_row is not None
                                     and from_trial == 0),
            )

        # 6. Commit trial spend first, then lots (FEFO order), then the
        #    (unchanged) new-wallet and legacy commits.
        if from_trial:
            trial_before = trial_row.limit - trial_row.used
            trial_row.used += from_trial
            trial_row.save(update_fields=['used', 'updated_at'])
            TrialUsageLog.objects.create(
                user_id=user_id, service=service, entry_type='debit',
                amount=-from_trial, balance_before=trial_before,
                balance_after=trial_row.limit - trial_row.used,
                ref_type=ref_type, ref_id=str(ref_id), spend_id=spend_id,
                description=description or
                    f"Used {from_trial} trial {SERVICE_LABELS[service]} credits",
            )

        for lot, n in lots_consumed:
            before = lot.quantity_remaining
            lot.quantity_remaining = before - n
            lot.quantity_used      = (lot.quantity_used or 0) + n
            lot.save(update_fields=['quantity_remaining', 'quantity_used', 'updated_at'])
            CreditAuditLog.objects.create(
                user_id=user_id, credit_type=service, entry_type='debit',
                amount=-n, balance_before=before, balance_after=lot.quantity_remaining,
                ref_type=ref_type, ref_id=str(ref_id), lot=lot, service=service,
                spend_id=spend_id,
                description=description or
                    f"Used {n} {SERVICE_LABELS[service]} credits (lot #{lot.id})",
            )

        if from_new:
            before = row.balance
            row.balance    = before - from_new
            row.total_used = (row.total_used or 0) + from_new
            row.save(update_fields=['balance', 'total_used', 'updated_at'])
            CreditAuditLog.objects.create(
                user_id=user_id, credit_type=service, entry_type='debit',
                amount=-from_new, balance_before=before, balance_after=row.balance,
                ref_type=ref_type, ref_id=str(ref_id), service=service,
                spend_id=spend_id,
                description=description or f"Used {from_new} {SERVICE_LABELS[service]} credits",
            )

        if from_legacy:
            before = getattr(cc, f'{pool}_current_credits') or 0
            setattr(cc, f'{pool}_current_credits', before - from_legacy)
            setattr(cc, f'{pool}_used_credits',
                    (getattr(cc, f'{pool}_used_credits') or 0) + from_legacy)
            cc.save(update_fields=[f'{pool}_current_credits', f'{pool}_used_credits'])
            CreditAuditLog.objects.create(
                user_id=user_id, credit_type=pool, entry_type='debit',
                amount=-from_legacy, balance_before=before,
                balance_after=before - from_legacy,
                ref_type=ref_type, ref_id=str(ref_id), service=service,
                spend_id=spend_id,
                description=(description or f"Used {from_legacy} credits") +
                            f" (legacy {pool.upper()} pool)",
            )
            UsedCredits.objects.create(
                user_id=user_id,
                **{f'{pool}_used_credits': from_legacy, f'{pool}_used_date': now_utc},
            )

    return spend_id


def charge_ev_bulk_file(file_entry, quantity, ref_type='validation', description=''):
    """Idempotent, exactly-once EV bulk credit charge shared by
    views/email_validation.py::verify_emails (charges at upload/start) and
    manage_credits() above (charges at download, if start never did).

    One billable operation — one uploaded file — gets one stable spend_id
    derived from the file itself (never from the calling request), so
    retries or concurrent hits from either entry point collapse onto the
    same identity instead of each charging independently.

    Locks the ListFiles row FIRST and re-checks credite_status under that
    lock, closing two pre-existing gaps: the start-path's charge and its
    credite_status update used to be two separate, non-atomic statements
    (a crash between them left the charge taken but the file unmarked, so
    the next attempt charged again), and the download-path's
    credite_status check was a plain unlocked read (two concurrent
    downloads of the same file could both pass it before either wrote the
    flag). Both are now impossible: everything happens inside one
    transaction, behind one row lock.

    Returns the spend_id used, or None if the file was already credited
    (by this call or an earlier one) and nothing further was done.
    Propagates InsufficientCredits uncaught, same as calling
    deduct_service_credits() directly — existing callers already handle it.
    """
    quantity = int(quantity or 0)
    spend_id = f"ev_bulk_file:{file_entry.file_id}"
    with transaction.atomic():
        locked = ListFiles.objects.select_for_update().get(pk=file_entry.pk)
        if locked.credite_status == "Credited":
            return None
        deduct_service_credits(
            locked.user_id, 'email_validation', quantity,
            ref_type=ref_type, ref_id=str(locked.file_id),
            description=description, spend_id=spend_id,
        )
        locked.credite_status = "Credited"
        locked.save(update_fields=['credite_status'])
    return spend_id


def refund_service_credits(user_id, service, count, ref_type='', ref_id='',
                           description='', spend_id=None):
    """Return credits for a failed/reversed action.

    Phase 3: source-aware for the ServiceCreditLot portion only. When
    `spend_id` is given and identifies debit(s) that drew from one or more
    lots (CreditAuditLog rows with `lot` set), the refunded amount for those
    rows goes back to the SAME lot(s) — quantity_remaining up,
    quantity_used down, invariant (purchased == remaining+used+expired+
    revoked) preserved exactly, since neither purchased/expired/revoked nor
    the total ever changes. A lot that has since expired or been revoked is
    left alone (its credit is genuinely gone, not resurrected); whatever
    isn't restorable to a lot falls through to the wallet below.

    Everything else — no spend_id given, a spend_id with no lot component,
    trial-funded and legacy-funded amounts — is refunded to the wallet
    exactly as before Phase 3. Trial's own refund semantics are
    deliberately NOT redesigned here (out of this phase's scope): a
    trial-funded spend still refunds to the wallet, unchanged.

    Old-credit retirement: the wallet/legacy slice of that same fallback is
    now explicitly capped at 0 while LEGACY_BALANCES_SPENDABLE is False —
    a debit that (historically, pre-retirement) drew from the wallet or the
    legacy CurrentCredits pool is no longer restored by this function, since
    that balance is retired and must not be added to again. This has no
    effect on lot refunds (untouched, above) or on the trial-funded portion
    of this same fallback (still restored exactly as before — trial refund
    behavior is out of scope for this fix and unchanged). In practice, no
    NEW debit can be wallet/legacy-funded any more at all (deduct_service_
    credits() no longer draws from either while the flag is False), so this
    only ever forecloses restoring a PRE-retirement wallet/legacy debit.

    Idempotent per debit row: each lot debit's refundable capacity is
    capped at `abs(debit.amount) - Sum(refunds that reverse it)`, so calling
    this twice for the same spend_id can never refund the same lot amount
    twice, and can never inflate the wallet from a lot. The wallet/legacy/
    trial fallback below is capped the same way when `spend_id` is given:
    at `Sum(non-lot debits for spend_id) - Sum(prior non-lot refunds for
    spend_id)`, correlated by spend_id rather than by a single `reverses`
    FK (one fallback refund can restore several original debit rows —
    trial, wallet, legacy — at once, so no single row can hold the
    reverse-link). Calling this twice for the same spend_id therefore
    cannot credit the wallet twice for that portion either. Callers that
    omit `spend_id` keep the pre-Phase-3 behavior exactly: `count` is
    trusted as given, with no capacity check.

    Never grows the legacy pool — unchanged from before Phase 3.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    count = int(count or 0)
    if count <= 0:
        return

    with transaction.atomic():
        # Lock order matches deduct_service_credits(): ServiceCredit anchor
        # first, unconditionally — even a refund that ends up fully
        # lot-restored still takes this lock, so a concurrent deduct (which
        # always locks ServiceCredit before any lot) can never form a cycle
        # against a concurrent refund locking a lot before ServiceCredit.
        # This same anchor lock is also what makes the plain (unlocked)
        # aggregate reads below safe: a second concurrent refund for the
        # same (user, service) cannot even reach them until this one has
        # committed or rolled back, so it always sees this call's writes.
        ServiceCredit.objects.get_or_create(user_id=user_id, service=service)
        wallet_row = ServiceCredit.objects.select_for_update().get(
            user_id=user_id, service=service)

        remaining = count

        if spend_id:
            lot_debit_rows = list(
                CreditAuditLog.objects.select_for_update()
                    .filter(user_id=user_id, spend_id=spend_id, entry_type='debit',
                            lot__isnull=False)
                    .order_by('id')
            )
            for debit_row in lot_debit_rows:
                if remaining <= 0:
                    break
                already_refunded = CreditAuditLog.objects.filter(
                    reverses=debit_row).aggregate(total=Sum('amount'))['total'] or 0
                capacity = abs(debit_row.amount) - already_refunded
                if capacity <= 0:
                    continue
                n = min(capacity, remaining)

                lot = ServiceCreditLot.objects.select_for_update().get(pk=debit_row.lot_id)
                if lot.status != ServiceCreditLot.STATUS_ACTIVE:
                    # Expired/revoked since the spend — that credit is gone;
                    # do not resurrect it, and do not let it spill into the
                    # wallet fallback below either (that fallback is capped
                    # to the ORIGINAL non-lot debit total, so a skipped
                    # lot's amount is simply forfeited here, matching "do
                    # not resurrect" for the wallet side too).
                    continue

                before = lot.quantity_remaining
                lot.quantity_remaining = before + n
                lot.quantity_used      = (lot.quantity_used or 0) - n
                lot.save(update_fields=['quantity_remaining', 'quantity_used', 'updated_at'])
                CreditAuditLog.objects.create(
                    user_id=user_id, credit_type=service, entry_type='refund',
                    amount=n, balance_before=before, balance_after=lot.quantity_remaining,
                    ref_type=ref_type, ref_id=str(ref_id), lot=lot, service=service,
                    spend_id=spend_id, reverses=debit_row,
                    description=description or
                        f"Refunded {n} {SERVICE_LABELS[service]} credits to lot #{lot.id}",
                )
                remaining -= n

        if remaining > 0:
            fallback_amount = remaining
            if spend_id:
                # Idempotency for the wallet/legacy/trial fallback: cap at
                # what this spend_id actually debited from non-lot sources,
                # minus whatever has already been refunded against it.
                # Correlated by spend_id (not `reverses`, which is a
                # one-to-one link and can't represent "restores parts of
                # several original rows at once").
                #
                # Old-credit retirement: the wallet/legacy slice is only
                # counted as restorable while LEGACY_BALANCES_SPENDABLE is
                # True -- split out separately from the trial slice (which
                # is unaffected) rather than disabling the whole fallback,
                # so a trial-funded debit still refunds to the wallet
                # exactly as before.
                wallet_legacy_debited = abs(
                    CreditAuditLog.objects.filter(
                        user_id=user_id, spend_id=spend_id, entry_type='debit',
                        lot__isnull=True,
                    ).aggregate(total=Sum('amount'))['total'] or 0
                )
                trial_debited = abs(
                    TrialUsageLog.objects.filter(
                        user_id=user_id, spend_id=spend_id, entry_type='debit',
                    ).aggregate(total=Sum('amount'))['total'] or 0
                )
                non_lot_debited = trial_debited + (
                    wallet_legacy_debited if LEGACY_BALANCES_SPENDABLE else 0)
                already_refunded_non_lot = CreditAuditLog.objects.filter(
                    user_id=user_id, spend_id=spend_id, entry_type='refund',
                    lot__isnull=True,
                ).aggregate(total=Sum('amount'))['total'] or 0
                non_lot_capacity = non_lot_debited - already_refunded_non_lot
                fallback_amount = min(fallback_amount, max(0, non_lot_capacity))

            if fallback_amount > 0:
                before = wallet_row.balance
                wallet_row.balance     = before + fallback_amount
                wallet_row.total_used  = max(0, (wallet_row.total_used or 0) - fallback_amount)
                wallet_row.save(update_fields=['balance', 'total_used', 'updated_at'])
                CreditAuditLog.objects.create(
                    user_id=user_id, credit_type=service, entry_type='refund',
                    amount=fallback_amount, balance_before=before, balance_after=wallet_row.balance,
                    ref_type=ref_type, ref_id=str(ref_id), service=service,
                    spend_id=spend_id or '',
                    description=description or
                        f"Refunded {fallback_amount} {SERVICE_LABELS[service]} credits",
                )
