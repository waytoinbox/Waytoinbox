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


def update_or_insert_current_credit(user_id, current_credit):
    obj, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
    obj.vc_current_credits = current_credit
    obj.save()


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


def insert_ip_credits(request, user_id, ip_credit):
    return insert_ac_credits(request, user_id, ip_credit, ref_type='subscription')


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
    """Reset AC and CC credits to 0 on subscription expiry.

    AC and CC are subscription-only credit types — no PAYG path exists for
    either. On expiry the full balance is cleared. Logs both resets to
    CreditAuditLog with entry_type='expired'.
    """
    ref_id    = str(sub.order_id or sub.pk)
    plan_name = sub.subs_plan or 'Subscription'

    with transaction.atomic():
        obj, _ = CurrentCredits.objects.select_for_update().get_or_create(user_id=user_id)

        before_ac = obj.ac_current_credits or 0
        before_cc = obj.cc_current_credits or 0

        obj.ac_current_credits = 0
        obj.cc_current_credits = 0
        obj.save(update_fields=['ac_current_credits', 'cc_current_credits'])

        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='ac', entry_type='expired',
            amount=-before_ac,
            balance_before=before_ac, balance_after=0,
            ref_type='subscription', ref_id=ref_id,
            description=f"AC credits reset on {plan_name} plan expiry",
        )
        CreditAuditLog.objects.create(
            user_id=user_id,
            credit_type='cc', entry_type='expired',
            amount=-before_cc,
            balance_before=before_cc, balance_after=0,
            ref_type='subscription', ref_id=ref_id,
            description=f"CC credits reset on {plan_name} plan expiry",
        )


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

    current_credit = get_vc_current_credit(user_id)
    if row_count > current_credit:
        logger.warning(f"Insufficient credits: {current_credit} available, {row_count} required.")
        return str(row_count)

    deduct_vc_credits(
        user_id, row_count,
        ref_type='validation',
        description=f"Bulk download: {table_name}",
    )
    file_entry.credite_status = "Credited"
    file_entry.save()

    return _fetch_rows(file_entry, selected_option)
