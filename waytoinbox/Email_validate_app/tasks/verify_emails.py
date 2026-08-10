import re
import os
import time
import socket
import smtplib
import logging
import pandas as pd
import dns.resolver

from typing import List, Optional, Tuple
from celery import shared_task, group, chord
from Email_validate_app.tasks.base import LoggedTask
from django.db import connection, transaction
from django.conf import settings
from django.utils import timezone
from Email_validate_app.models import ListFiles, AllEmails
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.mailer import send_job_failure_alert
from django.utils import timezone
from django.utils.text import get_valid_filename

import logging

logger = logging.getLogger(__name__)

# ---------------------- Constants ----------------------
MAX_RETRIES = 2
FROM_ADDRESS = "support@waytoinbox.com"
HELO_DOMAIN = "waytoinbox.com"  # your domain for HELO
CACHE_EXPIRATION = 3600
# INF-11: keep uploads outside MEDIA_ROOT so nginx never serves them directly
UPLOAD_FOLDER = str(settings.PRIVATE_UPLOAD_ROOT)

# ---------------------- Caches ----------------------
from django.core.cache import cache
email_validation_cache = {}  # keep for local fallback

SUPPORTED_EXTENSIONS = [".csv", ".xlsx", ".json", ".txt"]
RESULT_COLUMN_1 = "validation_results"
RESULT_COLUMN_2 = "result_reasons"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
# ---------------------- Format & Network Checks ----------------------

def is_valid_format_(email: str) -> bool:
    email_regex = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    return bool(email_regex.match(email))


def is_network_available(host="8.8.8.8", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False


def get_mx_records_(domain: str) -> Optional[List[str]]:
    cache_key = f"mx:{domain}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached != "NONE" else None

    resolvers = ["8.8.8.8", "1.1.1.1", "208.67.222.222"]
    for resolver in resolvers:
        try:
            dns.resolver.default_resolver = dns.resolver.Resolver()
            dns.resolver.default_resolver.nameservers = [resolver]
            answers = dns.resolver.resolve(domain, "MX", lifetime=10)
            mx_records = sorted(answers, key=lambda r: r.preference)
            mx_list = [str(record.exchange).rstrip(".") for record in mx_records]
            cache.set(cache_key, mx_list, timeout=3600)
            return mx_list
        except Exception:
            continue

    cache.set(cache_key, "NONE", timeout=3600)
    return None


# ---------------------- Provider Rule (Fallback) ----------------------

def provider_rule(email: str) -> str:
    try:
        email = email.strip()
        local, domain = email.lower().split("@")
        local = local.strip()
    except Exception:
        return "Invalid"

    providers = ["yahoo.com", "aol.com", "outlook.com", "hotmail.com"]
    if domain not in providers:
        return "Invalid"

    length = len(local)
    if 7 <= length <= 12:
        return "Valid"
    elif 1 <= length <= 6 or 13 <= length <= 20:
        return "Risky"
    else:
        return "Invalid"


# ---------------------- SMTP Check ----------------------

def check_smtp_with_retries_(email: str, mx_servers: List[str], retries: int = MAX_RETRIES) -> Tuple[str, str]:
    domain = email.split('@')[-1].lower().strip()
    last_error = None
    smtp_failed = True

    for attempt in range(retries):
        for mx in mx_servers:
            try:
                with smtplib.SMTP(mx, timeout=3) as server:
                    server.helo(HELO_DOMAIN)
                    server.mail(FROM_ADDRESS)
                    code, message = server.rcpt(email)

                    smtp_failed = False
                    message = message.decode().lower() if isinstance(message, bytes) else str(message).lower()

                    if code in [250, 251, 252]:
                        return "Valid", "Accepted by server"

                    if code in [550, 551, 553, 554] or \
                       "user unknown" in message or \
                       "no such user" in message or \
                       "does not exist" in message:
                        return "Invalid", "Mailbox not found"

                    if code in [421, 450, 451, 452] or \
                       "temporarily deferred" in message or \
                       "try again later" in message or \
                       "greylisted" in message:
                        return "Risky", "Temporary issue, try later"

            except Exception as e:
                last_error = str(e)
                time.sleep(1)

    # Fallback for known providers if SMTP failed
    try:
        providers = ["yahoo.com", "aol.com", "outlook.com", "hotmail.com"]
        if smtp_failed and domain in providers:
            rule_result = provider_rule(email)
            logger.debug("FALLBACK: %s %s", domain, rule_result)

            if rule_result == "Valid":
                return "Valid", "Rule-based (SMTP failed fallback)"
            elif rule_result == "Risky":
                return "Risky", "Rule-based (SMTP failed fallback)"
            else:
                return "Invalid", "Rule-based (SMTP failed fallback)"
    except Exception:
        pass

    return "Risky", "SMTP check failed after retries"


# ---------------------- Additional Checks ----------------------

def is_catch_all(domain: str, mx_records: List[str]) -> bool:
    cache_key = f"catchall:{domain}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    test_email = f"random-nonexistent-{domain}@{domain}"
    result = check_smtp_with_retries_(test_email, mx_records)[0] == "Valid"
    cache.set(cache_key, result, timeout=3600)
    return result


def is_role_based(email: str) -> bool:
    role_keywords = {"admin", "info", "support", "sales", "billing"}
    return any(word in email.split("@")[0] for word in role_keywords)


def is_disposable_email(email: str) -> bool:
    DISPOSABLE_DOMAINS = {"mailinator.com", "10minutemail.com", "tempmail.com", "guerrillamail.com"}
    domain = email.split('@')[1]
    return domain in DISPOSABLE_DOMAINS


def is_blacklisted(domain: str) -> bool:
    BLACKLISTED_DOMAINS = {"spamtrap.com", "blacklisteddomain.com", "bademail.com"}
    return domain in BLACKLISTED_DOMAINS


def is_suspicious(email: str) -> bool:
    suspicious_patterns = {"123", "test", "fake", "noreply", "random"}
    return any(p in email.lower() for p in suspicious_patterns)


# ---------------------- Core Per-Email Validation ----------------------

def validate_email_(email: str) -> Tuple[str, str]:
    if not is_valid_format_(email):
        return "Invalid", "Invalid email format"

    if email in email_validation_cache:
        cached_result, cached_reason, timestamp = email_validation_cache[email]
        if time.time() - timestamp < CACHE_EXPIRATION:
            return cached_result, cached_reason

    try:
        domain = email.split('@')[1]
    except IndexError:
        return "Invalid", "Invalid email format (missing domain)"

    if is_role_based(email):
        return "Others", "Role-based email detected"
    if is_disposable_email(email):
        return "Risky", "Disposable email domain"
    if is_blacklisted(domain):
        return "Others", "Domain is blacklisted"
    if is_suspicious(email):
        return "Risky", "Suspicious pattern detected"

    mx_servers = get_mx_records_(domain)
    if not mx_servers:
        return "Invalid", "No MX records found"

    if is_catch_all(domain, mx_servers):
        return "Risky", "Catch-all domain detected"

    result, reason = check_smtp_with_retries_(email, mx_servers)
    email_validation_cache[email] = (result, reason, time.time())
    return result, reason


# ---------------------- Database Utilities ----------------------



def update_listfile_status(table_name, status):
    logger.info("[%s] Status → %s", table_name, status)

    update_fields = {"job_status": status}

    if status == "Processing":
        update_fields["started_at"] = timezone.now()

    if status in ["Stopped", "Error"]:
        update_fields["completed_at"] = timezone.now()

    updated = ListFiles.objects.filter(table_name=table_name).update(**update_fields)
    logger.info("[%s] DB rows updated: %d", table_name, updated)


def update_listfile_counts(table_name, table_df):

    stats = table_df["validation_results"].value_counts().to_dict()
    total = len(table_df)
    file_obj = ListFiles.objects.get(table_name=table_name)

    file_obj.total_count      = total
    file_obj.valid_count      = stats.get("Valid", 0)
    file_obj.invalid_count    = stats.get("Invalid", 0)
    file_obj.unknown_count    = stats.get("Risky", 0)
    others = total - file_obj.valid_count - file_obj.invalid_count - file_obj.unknown_count
    file_obj.others_count     = others

    file_obj.valid_percentage   = round((file_obj.valid_count / total) * 100, 2) if total else 0.0
    file_obj.invalid_percentage = round((file_obj.invalid_count / total) * 100, 2) if total else 0.0
    file_obj.unknown_percentage = round((file_obj.unknown_count / total) * 100, 2) if total else 0.0
    file_obj.others_percentage  = round((others / total) * 100, 2) if total else 0.0

    file_obj.job_status    = "Complete"
    file_obj.completed_at  = timezone.now()   # ← Record completion time

    file_obj.save()


# ---------------------- Celery Tasks ----------------------

@shared_task(bind=True, max_retries=3, base=LoggedTask)
def validate_email_list_task(self, table, file_path, email_column, user_email="", user_name=""):
    """
    Entry point — splits CSV into chunks, fans out parallel chunk tasks.
    chord ensures finalize_validation_task runs only after ALL chunks done.
    """
    try:
        update_listfile_status(table, "Processing")

        chunk_jobs = []
        chunk_size = getattr(settings, 'EMAIL_VALIDATION_CHUNK_SIZE', 500)
        for chunk in pd.read_csv(file_path, chunksize=chunk_size, dtype=str):
            chunk = chunk.dropna(subset=[email_column])
            if chunk.empty:
                continue
            rows = list(zip(chunk["ivc_id"], chunk[email_column].str.strip()))
            chunk_jobs.append(validate_chunk_task.s(rows, table))

        if not chunk_jobs:
            update_listfile_status(table, "Stopped")
            return

        chord(
            group(chunk_jobs),
            finalize_validation_task.s(table, file_path, email_column, user_email, user_name)
        ).delay()

    except Exception as exc:
        update_listfile_status(table, "Stopped")
        logger.error("[%s] Entry task failed: %s", table, exc, exc_info=True)
        send_job_failure_alert(
            f"Bulk Email Validation (validate_email_list_task)",
            exc,
            context={"Job": table, "File": file_path},
        )
        raise self.retry(exc=exc, countdown=10)


from concurrent.futures import ThreadPoolExecutor, as_completed

@shared_task(bind=True, max_retries=3, soft_time_limit=900, time_limit=1000, base=LoggedTask)
def validate_chunk_task(self, rows, table):
    """Validates one chunk of (ivc_id, email) pairs — parallel SMTP."""
    results = {}

    def process(ivc_id, email):
        try:
            result, reason = validate_email_(email)
        except Exception as e:
            result, reason = "Risky", f"Unexpected error: {e}"
        logger.info(f"Validating {table} -- {ivc_id}: {email} -> {result} | {reason}")
        return ivc_id, {"email": email, "validation_results": result, "result_reasons": reason}

    with ThreadPoolExecutor(max_workers=getattr(settings, 'EMAIL_VALIDATION_MAX_WORKERS', 50)) as executor:
        futures = {executor.submit(process, ivc_id, email): ivc_id for ivc_id, email in rows}
        for future in as_completed(futures):
            ivc_id, data = future.result()
            results[ivc_id] = data

    return results


@shared_task(bind=True, time_limit=7200, soft_time_limit=6900, base=LoggedTask)
def finalize_validation_task(self, chunk_results, table, file_path, email_column, user_email="", user_name=""):
    """
    Chord callback — runs after ALL chunk tasks finish.
    Merges results, updates DB + CSV, marks job Complete.
    """
    try:
        validated_data = {}
        for chunk in chunk_results:
            validated_data.update(chunk)

        # Build df with all original columns + results
        df = pd.read_csv(file_path, dtype=str)
        df["validation_results"] = df["ivc_id"].map(
            {k: v["validation_results"] for k, v in validated_data.items()}
        )
        df["result_reasons"] = df["ivc_id"].map(
            {k: v["result_reasons"] for k, v in validated_data.items()}
        )
        df.to_csv(file_path, index=False, encoding="utf-8")

        # Upsert into AllEmails — Win_Id first, then original cols, reason last
        try:
            file_obj = ListFiles.objects.get(table_name=table)
            # Exclude internal/result columns
            skip_cols = {
                "ivc_id",
                "validation_results", "result_reasons",   # plural (added by finalize)
                "validation_result",  "result_reason",    # singular (added by safe.py create_job)
            }
            bulk_objs = []
            now = timezone.now()
            for _, row in df.iterrows():
                email_val = str(row.get(email_column, "")).strip()
                if not email_val or email_val.lower() == "nan":
                    continue
                # Win_Id first, then all original columns (preserving CSV order), reason last
                extra = {"Win_Id": str(row.get("ivc_id", ""))}
                for col in df.columns:
                    if col not in skip_cols:
                        val = row.get(col)
                        if pd.notna(val):
                            extra[col] = str(val)
                extra["reason"] = str(row.get("result_reasons", "") or "")
                bulk_objs.append(AllEmails(
                    file_id=file_obj.file_id,
                    user=file_obj.user,
                    table_name=table,
                    email=email_val,
                    validation_results=str(row.get("validation_results", "") or "") or None,
                    completed_time=now,
                    extra_data=extra,
                ))
            if bulk_objs:
                AllEmails.objects.bulk_create(
                    bulk_objs,
                    update_conflicts=True,
                    update_fields=["validation_results", "extra_data", "completed_time"],
                )
                logger.info("[%s] AllEmails upserted: %d rows", table, len(bulk_objs))
        except Exception as ae:
            logger.error("[%s] AllEmails upsert failed: %s", table, ae, exc_info=True)

        update_listfile_counts(table, df)
        logger.info("[%s] Finalized %d emails.", table, len(validated_data))

        if user_email:
            # Always save in-app notification
            try:
                from Email_validate_app.models import UserTable as _UserTable
                from Email_validate_app.utils import create_notification
                _u_n = _UserTable.objects.filter(user_email=user_email).only('id').first()
                _f_n = ListFiles.objects.get(table_name=table)
                if _u_n:
                    create_notification(_u_n.id, 'job_complete',
                        f"Bulk job #{_f_n.file_id} finished — {_f_n.total_count or 0} emails verified",
                        url='/services/')
            except Exception as notif_err:
                logger.error("[%s] In-app notification failed: %s", table, notif_err)

            # Send email only if toggle is on
            try:
                from Email_validate_app.models import UserTable as _UserTable2
                from Email_validate_app.services.mailer import send_job_completed_email
                _u_e = _UserTable2.objects.filter(user_email=user_email).only('notify_job_complete').first()
                if not _u_e or _u_e.notify_job_complete:
                    _f_e = ListFiles.objects.get(table_name=table)
                    created_at = _f_e.insert_date.strftime("%d %b %Y, %I:%M %p UTC") if _f_e.insert_date else ""
                    send_job_completed_email(
                        user_name=user_name,
                        user_email=user_email,
                        job_id=_f_e.file_id,
                        file_name=_f_e.file_name or table,
                        created_at=created_at,
                        total=_f_e.total_count or 0,
                    )
            except Exception as mail_err:
                logger.error("[%s] Job completion email failed: %s", table, mail_err)

    except Exception as e:
        update_listfile_status(table, "Stopped")
        logger.error("[%s] Finalize failed: %s", table, e, exc_info=True)
        send_job_failure_alert(
            f"Bulk Email Validation Finalize (finalize_validation_task)",
            e,
            context={"Job": table, "File": file_path},
        )
        raise


# ---------------------- File Utilities ----------------------

def secure_filename(filename):
    
    filename = get_valid_filename(filename)
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename


def read_file(file_path, file_ext):
    try:
        if file_ext == ".csv":
            df = pd.read_csv(file_path, encoding="utf-8")
        elif file_ext == ".xlsx":
            df = pd.read_excel(file_path, engine="openpyxl")
        elif file_ext == ".json":
            df = pd.read_json(file_path)
        elif file_ext == ".txt":
            df = pd.read_csv(file_path, delimiter="\t", encoding="utf-8")
        else:
            raise ValueError("Unsupported file format.")

        df.dropna(axis=1, how="all", inplace=True)
        return df

    except Exception as e:
        raise ValueError(f"Error reading file: {e}")
    
    

def find_emailcolumn_file(file_path):
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            logger.warning("Unsupported file format: %s", file_path)
            return None

        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        email_col = None
        max_email_count = 0

        for col in df.columns:
            valid_emails = df[col].astype(str).str.match(email_pattern, na=False).sum()
            if valid_emails > max_email_count:
                max_email_count = valid_emails
                email_col = col

        return email_col

    except Exception as e:
        logger.error("Error finding email column: %s", e)
        return None


# ---------------------- Job Creation ----------------------

def create_job(file, request):
    import pytz
    from datetime import datetime as _datetime
    try:
        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        # SEC-12: reject oversized uploads before writing to disk
        if hasattr(file, 'size') and file.size > MAX_UPLOAD_BYTES:
            raise ValueError(f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB).")

        filename = secure_filename(file.name)
        file_ext = os.path.splitext(filename)[1].lower()

        if file_ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file format: {file_ext}. Supported: {', '.join(SUPPORTED_EXTENSIONS)}")

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        current_dt = _datetime.utcnow().replace(tzinfo=pytz.UTC)
        userid = get_user_id(request)

        list_file = ListFiles.objects.create(
            file_name=filename,
            user_id=userid,
            insert_date=current_dt,
        )
        file_id = list_file.file_id

        sanitized_table_name = f"WIN_{file_id}_{current_dt.strftime('%Y_%m_%d')}"
        sanitized_table_name = re.sub(r'\W|^(?=\d)', '_', sanitized_table_name)

        df = read_file(file_path, file_ext)
        if df.empty:
            raise ValueError("The uploaded file is empty.")

        _email_pat = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        email_col = next(
            (c for c in df.columns if df[c].astype(str).str.match(_email_pat, na=False).sum() > 0),
            None,
        )
        if email_col and email_col != "_Emails_":
            df.rename(columns={email_col: "_Emails_"}, inplace=True)

        df.insert(0, "ivc_id", range(1, len(df) + 1))
        df[RESULT_COLUMN_1] = " "
        df[RESULT_COLUMN_2] = " "

        file_path_csv = os.path.join(UPLOAD_FOLDER, f"{sanitized_table_name}.csv")
        df.to_csv(file_path_csv, index=False)

        df = df.where(pd.notnull(df), None)

        column_types = []
        for col in df.columns:
            col_name = re.sub(r'\W|^(?=\d)', '_', col)
            col_type = "TEXT"
            if df[col].dtype == 'int64':
                col_type = "BIGINT"
            elif df[col].dtype == 'float64':
                col_type = "DOUBLE"
            column_types.append(f"`{col_name}` {col_type}")

        create_table_query = f"CREATE TABLE `{sanitized_table_name}` ({', '.join(column_types)});"
        with connection.cursor() as cursor:
            cursor.execute(create_table_query)

        placeholders = ", ".join(["%s"] * len(df.columns))
        insert_query = f"INSERT INTO `{sanitized_table_name}` VALUES ({placeholders})"
        records = [tuple(None if pd.isna(x) else x for x in row) for row in df.to_numpy()]
        with connection.cursor() as cursor:
            cursor.executemany(insert_query, records)

        logger.info("Job created: %s (%d rows)", sanitized_table_name, len(records))
        return sanitized_table_name, file_id, "Job Created"

    except Exception as e:
        logger.error("Error in Job Creation: %s", e, exc_info=True)
        return None, None, f"Error: {e}"