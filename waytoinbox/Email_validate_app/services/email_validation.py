import re
import asyncio
import logging
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)

import pytz
import dns.asyncresolver
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from Email_validate_app.models import (
    CurrentCredits, UsedCredits, EmailValidate, EmailValidationLog,
)
from Email_validate_app.services.credit_manager import (
    deduct_service_credits, refund_service_credits, InsufficientCredits,
)
from Email_validate_app.tasks.verify_emails import validate_email_, get_mx_records_


def core_validate_email(user_id, email, deduct_credits=False):
    """
    Core validation logic shared by the website (deduct_credits=True)
    and the API (deduct_credits=False).

    Phase 6 commit 1: credits are now taken from the email_validation service
    wallet (new balance first, then the legacy VC pool) and, crucially, BEFORE
    any work is done.

    This previously deducted after validating and swallowed the failure:

        except (ValueError, CurrentCredits.DoesNotExist):
            pass  # insufficient credits or no credits row - validation still proceeds

    which meant a user with a zero balance was validated for free every time.
    Now an insufficient balance returns {'need_credits': True} and no
    validation is performed at all. One email costs exactly one credit.
    """
    if deduct_credits:
        try:
            deduct_service_credits(
                user_id, 'email_validation', 1,
                ref_type='validation', ref_id=email,
                description='Single email validation')
        except InsufficientCredits as e:
            return {"error": str(e), "need_credits": True}

    try:
        result, reason = validate_email_(email)

        domain = email.split('@')[1] if '@' in email else ''
        mx_records = get_mx_records_(domain) if domain else []
        mx_record = mx_records[0] if mx_records else "None"

        current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)

        ev_record = EmailValidate.objects.create(
            user_id=user_id,
            email=email,
            mx_found=result,
            mx_record=mx_record,
            insert_date=current_datetime,
        )

        EmailValidationLog.objects.create(
            user_id=user_id,
            email=email,
        )

        return {
            "email":     email,
            "result":    result,
            "reason":    reason,
            "mx_record": mx_record,
            "record_id": ev_record.id,
            "error":     None,
        }

    except Exception as e:
        # The credit was taken before the work started, so a failure here must
        # give it back — otherwise a DNS timeout silently costs the user a
        # credit for a result they never received.
        if deduct_credits:
            try:
                refund_service_credits(
                    user_id, 'email_validation', 1,
                    ref_type='validation', ref_id=email,
                    description='Refund: single email validation failed')
            except Exception:
                logger.exception(
                    "Could not refund the validation credit for user %s (%s) "
                    "after validation failed", user_id, email)
        logger.warning("Single validation failed for user %s (%s): %s", user_id, email, e)
        return {"error": str(e)}


# ── Email syntax & domain helpers ─────────────────────────────────────────────

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')


def validate_email_syntax(email):
    return EMAIL_REGEX.match(email) is not None


def get_domain(email):
    return email.split('@')[1]


async def _check_domain(semaphore, domain):
    async with semaphore:
        try:
            await dns.asyncresolver.resolve(domain, 'A')
        except Exception:
            return domain, False
        try:
            records = await dns.asyncresolver.resolve(domain, 'MX')
            return domain, len(records) > 0
        except Exception:
            return domain, False


async def _check_all_domains(domains):
    semaphore = asyncio.Semaphore(getattr(settings, 'EMAIL_VALIDATION_CONCURRENCY', 150))
    tasks = [_check_domain(semaphore, d) for d in domains]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    domain_map = {}
    for r in results:
        if isinstance(r, tuple):
            domain_map[r[0]] = r[1]
    return domain_map


def validate_emails_in_parallel(emails, column_name):
    parsed = []
    unique_domains = set()
    for email_data in emails:
        email = str(email_data[column_name]) if email_data[column_name] else ""
        if not validate_email_syntax(email):
            parsed.append((email, None))
        else:
            domain = get_domain(email)
            unique_domains.add(domain)
            parsed.append((email, domain))

    loop = asyncio.new_event_loop()
    try:
        domain_map = loop.run_until_complete(_check_all_domains(list(unique_domains)))
    finally:
        loop.close()

    results = []
    invalid_count = 0
    for email, domain in parsed:
        if domain is None or not domain_map.get(domain, False):
            status = "Invalid"
            invalid_count += 1
        else:
            status = "Valid"
        results.append({"email": email, "status": status})

    invalid_percentage = round((invalid_count / len(emails)) * 100, 2) if emails else 0
    return results, invalid_percentage


_TABLE_NAME_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')


def find_email_column(table_name):
    # DB-01: reject table names that don't match the known pattern before any SQL
    if not _TABLE_NAME_RE.fullmatch(table_name or ''):
        logger.error("find_email_column: rejected invalid table name %r", table_name)
        return []

    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    email_columns = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            columns = cursor.fetchall()
            for column in columns:
                column_name = column[0]
                cursor.execute(f"SELECT `{column_name}` FROM `{table_name}` LIMIT 100")
                rows = cursor.fetchall()
                for row in rows:
                    value = str(row[0]) if row[0] is not None else ''
                    if re.match(email_regex, value):
                        email_columns.append(column_name)
                        break
        return email_columns
    except Exception as e:
        logger.error("Error finding email columns: %s", e)
        return []


def can_validate_email(user_id):
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    count_today = EmailValidationLog.objects.filter(
        user_id=user_id,
        validated_at__gte=today_start,
    ).count()
    return count_today < getattr(settings, 'EMAIL_VALIDATION_DAILY_LIMIT', 5)
