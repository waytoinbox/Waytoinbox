import secrets
import re
import logging
from datetime import datetime

import pytz
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Coalesce

from Email_validate_app.models import (
    CurrentCredits, TotalCredits, UsedCredits, CreditAuditLog, AllEmails, ListFiles,
    ServiceCredit, SERVICE_CHOICES, SERVICE_KEYS,
)

logger = logging.getLogger(__name__)


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

    # The charge and the credite_status flag share one transaction. Previously
    # a failure between them would have spent the credits while leaving the file
    # unmarked, so the next download attempt would charge for it again.
    try:
        with transaction.atomic():
            deduct_service_credits(
                user_id, 'email_validation', row_count,
                ref_type='validation',
                description=f"Bulk download: {table_name}",
            )
            file_entry.credite_status = "Credited"
            file_entry.save()
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
    """Not enough credits across the new wallet AND the legacy pool.

    Subclasses ValueError deliberately: every existing deduction call site
    already handles ValueError (that is what deduct_vc/ac/cc_credits raise),
    so swapping them onto the new API cannot silently break their error paths.
    """

    def __init__(self, service, needed, available):
        self.service   = service
        self.needed    = needed
        self.available = available
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
    """Balance in the NEW wallet only (excludes legacy)."""
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    row = ServiceCredit.objects.filter(user_id=user_id, service=service).first()
    return row.balance if row else 0


def get_effective_balance(user_id, service):
    """What the user can actually spend right now: new wallet + legacy pool.

    This is the number to gate actions on and to show next to a service. Note
    that for the four analysis services the legacy half is SHARED, so summing
    get_effective_balance() across them double-counts — see
    get_all_service_balances(), which reports the shared pool separately.
    """
    return get_service_balance(user_id, service) + _legacy_balance(user_id, service)


def get_all_service_balances(user_id):
    """All seven balances in ONE query (plus one for the legacy row).

    Used by the context processor on every authenticated request, so it must
    stay cheap and must never create rows.

    Returns:
        {
          'services': {service: {'new': int, 'legacy': int, 'effective': int}},
          'legacy_shared': {'ac': int, 'vc': int, 'cc': int},
        }

    `legacy_shared['ac']` is ONE pool backing four services. The UI must show
    it as a single shared figure, never as four independent balances, or a
    user with 100 AC appears to have 400.
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

    services = {}
    for service in SERVICE_KEYS:
        pool = SERVICE_LEGACY_POOL.get(service)
        new = new_balances.get(service, 0)
        leg = legacy.get(pool, 0) if pool else 0
        services[service] = {'new': new, 'legacy': leg, 'effective': new + leg}

    return {'services': services, 'legacy_shared': legacy}


def add_service_credits(user_id, service, amount, ref_type='service_purchase',
                        ref_id='', description=''):
    """Grant credits to a service wallet. Never expires.

    Only ever called from a verified payment or an admin adjustment.
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
                           description=''):
    """Spend `count` credits for `service`: new wallet first, then the legacy
    pool for any remainder.

    Worked example from the spec:
        new email_validation = 20, legacy vc = 100, request 50
        -> 20 from new, 30 from legacy
        -> new = 0, legacy = 70

    Atomicity: the whole read-modify-write is inside one transaction with
    select_for_update() on every row it touches, so concurrent spends cannot
    overdraw. Critically, the four analysis services lock the SAME
    CurrentCredits row, so two of them racing on a shared AC pool serialise
    correctly and cannot both spend the same credit.

    Lock order is fixed at ServiceCredit -> CurrentCredits everywhere in this
    module. Any future code touching both MUST use the same order or it can
    deadlock against this.

    Raises InsufficientCredits (a ValueError) without writing anything if the
    combined balance cannot cover the request — never partially deducts.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    count = int(count or 0)
    if count <= 0:
        return

    pool = SERVICE_LEGACY_POOL.get(service)
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

    with transaction.atomic():
        # 1. New wallet first.
        ServiceCredit.objects.get_or_create(user_id=user_id, service=service)
        row = ServiceCredit.objects.select_for_update().get(
            user_id=user_id, service=service)
        from_new = min(row.balance, count)
        remainder = count - from_new

        # 2. Legacy pool covers whatever is left.
        cc = None
        from_legacy = 0
        if remainder and pool:
            cc, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
            cc = CurrentCredits.objects.select_for_update().get(user_id=user_id)
            legacy_avail = getattr(cc, f'{pool}_current_credits', 0) or 0
            from_legacy = min(legacy_avail, remainder)
            remainder -= from_legacy

        if remainder > 0:
            # Nothing has been written yet — the transaction simply unwinds.
            raise InsufficientCredits(service, count, from_new + from_legacy)

        # 3. Commit both halves.
        if from_new:
            before = row.balance
            row.balance    = before - from_new
            row.total_used = (row.total_used or 0) + from_new
            row.save(update_fields=['balance', 'total_used', 'updated_at'])
            CreditAuditLog.objects.create(
                user_id=user_id, credit_type=service, entry_type='debit',
                amount=-from_new, balance_before=before, balance_after=row.balance,
                ref_type=ref_type, ref_id=str(ref_id),
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
                ref_type=ref_type, ref_id=str(ref_id),
                description=(description or f"Used {from_legacy} credits") +
                            f" (legacy {pool.upper()} pool)",
            )
            UsedCredits.objects.create(
                user_id=user_id,
                **{f'{pool}_used_credits': from_legacy, f'{pool}_used_date': now_utc},
            )


def refund_service_credits(user_id, service, count, ref_type='', ref_id='',
                           description=''):
    """Return credits to the new wallet (e.g. an action failed after charging).

    Always refunds to the NEW wallet even if the original spend came partly
    from legacy — legacy pools are drain-only by design and must never grow.
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    count = int(count or 0)
    if count <= 0:
        return

    with transaction.atomic():
        ServiceCredit.objects.get_or_create(user_id=user_id, service=service)
        row = ServiceCredit.objects.select_for_update().get(
            user_id=user_id, service=service)
        before = row.balance
        row.balance    = before + count
        row.total_used = max(0, (row.total_used or 0) - count)
        row.save(update_fields=['balance', 'total_used', 'updated_at'])

        CreditAuditLog.objects.create(
            user_id=user_id, credit_type=service, entry_type='refund',
            amount=count, balance_before=before, balance_after=row.balance,
            ref_type=ref_type, ref_id=str(ref_id),
            description=description or f"Refunded {count} {SERVICE_LABELS[service]} credits",
        )
