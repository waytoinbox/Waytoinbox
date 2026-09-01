import json
import os
import re
import uuid
import ipaddress
import logging
from urllib.parse import urlparse
from datetime import datetime

import pytz
import pandas as pd
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from Email_validate_app.models import (
    APIKey,
    AllEmails, ListFiles,
    BlocklistMonitor, BlacklistStatus, BlacklistListed,
    CurrentCredits, UsedCredits, UserTable, ServiceCredit,
    DomainBlocklist, DomainBlacklistStatus, DomainBlacklistListed,
)
from Email_validate_app.services.api_auth import api_key_required
from Email_validate_app.services.monitor import ip_blacklists, domain_blacklists
from Email_validate_app.services.email_analyzer import ProfessionalEmailAnalyzer
from Email_validate_app.utils import get_user_id
from Email_validate_app.views.blocklist import _AlreadyMonitored
from Email_validate_app.services.credit_manager import (
    get_vc_current_credit, deduct_ac_credits, get_effective_balance,
    deduct_service_credits, InsufficientCredits,
)
from Email_validate_app.views import get_current_credit, core_validate_email, check_spf, check_dmarc, check_dkim
from Email_validate_app.tasks.verify_emails import (
    validate_email_list_task,
    find_emailcolumn_file,
    secure_filename,
    read_file,
    UPLOAD_FOLDER,
    SUPPORTED_EXTENSIONS,
    RESULT_COLUMN_1,
    RESULT_COLUMN_2,
)

logger = logging.getLogger(__name__)


@csrf_exempt
def validate_email_view(request):
    try:
        return _validate_email_view_inner(request)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("validate_email_view unhandled error")
        return JsonResponse({"status": "error", "message": "Server error. Please try again."}, status=500)


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _validate_email_view_inner(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        try:
            api_key_obj = APIKey.objects.get(key=api_key_header, is_active=True)
            user_id = api_key_obj.user.id
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive API key"}, status=403)
    else:
        user_id = get_user_id(request)
        if not user_id:
            # Guest path: IP-based rate limit, no DB writes
            from django.core.cache import cache
            from django.conf import settings
            from Email_validate_app.tasks.verify_emails import validate_email_, get_mx_records_

            content_type = request.content_type or ""
            if "application/json" in content_type:
                try:
                    body = json.loads(request.body)
                    email = body.get("email", "").strip()
                except Exception:
                    return JsonResponse({"error": "Invalid JSON body"}, status=400)
            else:
                email = (request.POST.get("email") or request.POST.get("_email_", "")).strip()

            if not email:
                return JsonResponse({"status": "error", "message": "Email is required"}, status=400)

            ip = _get_client_ip(request)
            today = timezone.now().date().isoformat()
            cache_key = f'guest_ev:{ip}:{today}'
            count = cache.get(cache_key, 0)
            limit = getattr(settings, 'GUEST_EMAIL_VALIDATION_DAILY_LIMIT', 5)
            if count >= limit:
                return JsonResponse({
                    "status": "error",
                    "message": f"Daily free limit reached ({limit}/day). Sign up free for more verifications."
                }, status=429)

            result, reason = validate_email_(email)
            domain = email.split('@')[1] if '@' in email else ''
            mx_records = get_mx_records_(domain) if domain else []
            mx_record = mx_records[0] if mx_records else "None"

            cache.set(cache_key, count + 1, timeout=86400)

            try:
                from Email_validate_app.models import GuestActivity
                GuestActivity.objects.create(
                    ip_address=ip,
                    activity_type='email_verify',
                    input_value=email,
                    result=str(result),
                )
            except Exception:
                pass

            return JsonResponse({
                "status":    "ok",
                "email":     email,
                "result":    str(result),
                "reason":    str(reason or ""),
                "mx_record": str(mx_record),
                "record_id": None,
                "credits":   0,
            })

    content_type = request.content_type or ""
    if "application/json" in content_type:
        try:
            body = json.loads(request.body)
            email = body.get("email", "").strip()
        except Exception:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        email = (request.POST.get("email") or request.POST.get("_email_", "")).strip()

    if not email:
        return JsonResponse({"status": "error", "message": "Email is required"}, status=400)

    # Phase 6 commit 1: one email costs one credit here too. The old
    # "0 credits -> 5 free a day" branch is gone; without a balance the API
    # returns 402 and performs no validation.
    email_result = core_validate_email(user_id, email, deduct_credits=True)

    if email_result.get("need_credits"):
        return JsonResponse({
            "status":  "error",
            "message": "Insufficient Email Validation credits.",
            "need":    1,
            "current": get_effective_balance(user_id, 'email_validation'),
        }, status=402)

    if email_result.get("error"):
        return JsonResponse({"status": "error", "message": str(email_result["error"])}, status=500)

    return JsonResponse({
        "status":    "ok",
        "email":     email_result["email"],
        "result":    str(email_result["result"]),
        "reason":    str(email_result.get("reason", "") or ""),
        "mx_record": str(email_result["mx_record"]),
        "record_id": email_result.get("record_id"),
        # Effective balance, not the raw VC column: after the Phase 6 cutover a
        # user spending from the new email_validation wallet would otherwise be
        # told they have 0 credits left while still being able to validate.
        "credits":   get_effective_balance(user_id, 'email_validation'),
    })


@csrf_exempt
def domain_validate_api(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)

    content_type = request.content_type or ""
    if "application/json" in content_type:
        try:
            body = json.loads(request.body)
            domain        = body.get("domain", "").strip().lower()
            dkim_selector = body.get("dkim_selector", "").strip()
        except Exception:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        domain = (
            request.POST.get("domain_") or
            request.POST.get("_domain_") or
            request.POST.get("domain", "")
        ).strip().lower()
        dkim_selector = request.POST.get("dkim_selector", "").strip()

    if not domain:
        return JsonResponse({"status": "error", "message": "Please enter a domain"})

    from Email_validate_app.views import check_dkim_auto
    dkim_result  = check_dkim(domain, dkim_selector) if dkim_selector else check_dkim_auto(domain)
    spf_result   = check_spf(domain)
    dmarc_result = check_dmarc(domain)

    try:
        from Email_validate_app.models import GuestActivity
        overall = 'pass' if (
            spf_result.get('status') == 'pass' and
            dmarc_result.get('status') == 'pass'
        ) else 'fail'
        GuestActivity.objects.create(
            ip_address=_get_client_ip(request),
            activity_type='dmarc_check',
            input_value=domain,
            result=overall,
        )
    except Exception:
        pass

    return JsonResponse({
        "status": "ok",
        "domain": domain,
        "spf":    spf_result,
        "dmarc":  dmarc_result,
        "dkim":   dkim_result,
    })


@csrf_exempt
def ip_blocklist_check_api(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)

    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        try:
            api_key_obj = APIKey.objects.get(key=api_key_header, is_active=True)
            user_id = api_key_obj.user.id
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive API key"}, status=403)
    else:
        user_id = get_user_id(request)
        if not user_id:
            return JsonResponse({"status": "error", "message": "Login required"}, status=401)

    content_type = request.content_type or ""
    if "application/json" in content_type:
        try:
            body = json.loads(request.body)
            ip_s = body.get("ip", "").strip()
        except Exception:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        ip_s = (request.POST.get("Ip_s") or request.POST.get("ip", "")).strip()

    if not ip_s:
        return JsonResponse({"status": "error", "message": "IP address is required"}, status=400)

    try:
        ipaddress.ip_address(ip_s)
    except ValueError:
        return JsonResponse({"status": "error", "message": "Enter a valid IP address"}, status=400)

    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return JsonResponse({"status": "error", "message": "User not found"}, status=404)

    if BlocklistMonitor.objects.filter(user=user, ips=ip_s, is_hidden=False).exists():
        return JsonResponse({"status": "warning", "message": f"IP '{ip_s}' is already being monitored."})

    # Phase 6 commit 6: this endpoint ADDS a monitor (the duplicate check above
    # returns early for an already-monitored IP), so the charge is for the add,
    # not for a repeated check. Creation and the charge now share one
    # transaction — the old order deducted first and created second, so a failed
    # insert burned the credit. The lock is the same row, in the same order,
    # deduct_service_credits() takes, so two simultaneous adds of one IP
    # serialise and only one is charged.
    current_datetime = timezone.now().replace(tzinfo=None)
    try:
        with transaction.atomic():
            ServiceCredit.objects.select_for_update().filter(
                user_id=user_id, service='ip_blocklist',
            ).first()

            if BlocklistMonitor.objects.filter(user=user, ips=ip_s, is_hidden=False).exists():
                raise _AlreadyMonitored(ip_s)

            new_entry = BlocklistMonitor.objects.create(
                user=user, ips=ip_s, created_date=current_datetime)
            deduct_service_credits(user_id, 'ip_blocklist', 1,
                                   ref_type='ip_check', ref_id=ip_s,
                                   description='IP Blocklist Check')
    except _AlreadyMonitored:
        return JsonResponse({"status": "warning", "message": f"IP '{ip_s}' is already being monitored."})
    except CurrentCredits.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Credits not found for your account"}, status=402)
    except ValueError:
        return JsonResponse({"status": "error", "message": "No Analysis Credits left. Please upgrade your plan to continue."}, status=429)

    try:
        status = ip_blacklists(ip_s)
        if not isinstance(status, dict):
            status = {}
    except Exception:
        status = {}

    for blacklist_name, blacklist_status in status.items():
        try:
            BlacklistStatus.objects.create(
                ip_id=new_entry.ip_id, ips=ip_s,
                blacklists_name=blacklist_name, status=blacklist_status,
            )
            if blacklist_status == "Listed":
                BlacklistListed.objects.create(
                    ip_id=new_entry.ip_id, ips=ip_s,
                    blacklists_name=blacklist_name, status=blacklist_status,
                )
        except Exception:
            pass

    listed_count = sum(1 for v in status.values() if v == "Listed")
    BlocklistMonitor.objects.filter(ip_id=new_entry.ip_id).update(
        last_monitor_date=current_datetime, listed_count=str(listed_count)
    )

    remaining = get_vc_current_credit(user_id)
    return JsonResponse({
        "status": "ok",
        "ip": ip_s,
        "ip_id": new_entry.ip_id,
        "listed_count": listed_count,
        "ip_current_credits": remaining,
    })


@csrf_exempt
def domain_blocklist_check_api(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)

    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        try:
            api_key_obj = APIKey.objects.get(key=api_key_header, is_active=True)
            user_id = api_key_obj.user.id
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive API key"}, status=403)
    else:
        user_id = get_user_id(request)
        if not user_id:
            return JsonResponse({"status": "error", "message": "Login required"}, status=401)

    content_type = request.content_type or ""
    if "application/json" in content_type:
        try:
            body = json.loads(request.body)
            domain_s = body.get("domain", "").strip().lower()
        except Exception:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        domain_s = (request.POST.get("domain_s") or request.POST.get("domain", "")).strip().lower()

    if not domain_s:
        return JsonResponse({"status": "error", "message": "Domain is required"}, status=400)

    parsed = urlparse(domain_s)
    if parsed.netloc:
        domain_s = parsed.netloc

    domain_regex = r"^(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$"
    if not re.match(domain_regex, domain_s):
        return JsonResponse({"status": "error", "message": "Enter a valid domain"}, status=400)

    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return JsonResponse({"status": "error", "message": "User not found"}, status=404)

    if DomainBlocklist.objects.filter(user=user, domain=domain_s, is_hidden=False).exists():
        return JsonResponse({"status": "warning", "message": f"Domain '{domain_s}' is already being monitored."})

    current_datetime = timezone.now()
    try:
        deduct_ac_credits(user_id, 1, ref_type='ip_check', ref_id=domain_s, description='Domain Blocklist Check')
    except CurrentCredits.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Credits not found for your account"}, status=402)
    except ValueError:
        return JsonResponse({"status": "error", "message": "No Analysis Credits left. Please upgrade your plan to continue."}, status=429)

    try:
        new_entry = DomainBlocklist.objects.create(
            user=user, domain=domain_s,
            created_date=current_datetime, last_monitor_date=current_datetime, listed_count=0,
        )
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

    try:
        status = domain_blacklists(domain_s)
        if not isinstance(status, dict):
            status = {}
    except Exception:
        status = {}

    status_objs = []
    listed_objs = []
    for name, result in status.items():
        status_objs.append(DomainBlacklistStatus(
            domain=new_entry, domains=domain_s, blacklists_name=name, status=result,
        ))
        if result == "Listed":
            listed_objs.append(DomainBlacklistListed(
                domain=new_entry, domains=domain_s, blacklists_name=name, status=result,
            ))

    if status_objs:
        DomainBlacklistStatus.objects.bulk_create(status_objs)
    if listed_objs:
        DomainBlacklistListed.objects.bulk_create(listed_objs)

    listed_count = sum(1 for v in status.values() if v == "Listed")
    DomainBlocklist.objects.filter(domain_id=new_entry.domain_id).update(
        listed_count=listed_count, last_monitor_date=current_datetime,
    )

    return JsonResponse({
        "status": "ok",
        "domain": domain_s,
        "domain_id": new_entry.domain_id,
        "listed_count": listed_count,
        "ip_current_credits": get_vc_current_credit(user_id),
    })


def _validate_header_input(raw_data):
    if not raw_data or len(raw_data.strip()) < 30:
        return False, "Input too short"
    required = ["From:", "Received:", "Subject:", "Date:"]
    found = sum(1 for f in required if f.lower() in raw_data.lower())
    if found < 2:
        return False, "Invalid email/header format"
    return True, "Valid"


@csrf_exempt
def header_analysis_api(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)

    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        try:
            api_key_obj = APIKey.objects.get(key=api_key_header, is_active=True)
            user_id = api_key_obj.user.id
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive API key"}, status=403)
    else:
        user_id = get_user_id(request)
        if not user_id:
            return JsonResponse({"status": "error", "message": "Login required"}, status=401)

    # Phase 6 commit 5: the gate reads the header_analysis service wallet plus
    # the legacy AC pool behind it, instead of the raw AC column, so a user
    # whose credits live in the new wallet is not turned away. The message and
    # the 429 are unchanged.
    if get_effective_balance(user_id, 'header_analysis') <= 0:
        return JsonResponse({"status": "error", "message": "No Analysis Credits left. Please upgrade your plan to continue."}, status=429)

    content_type = request.content_type or ""
    final_email = ""

    if "application/json" in content_type:
        try:
            body = json.loads(request.body)
            final_email = body.get("header", "").strip()
        except Exception:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
    else:
        upload_file = request.FILES.get("header_file")
        raw_text = request.POST.get("header", "").strip()
        if upload_file:
            if upload_file.size > 50 * 1024 * 1024:
                return JsonResponse({"status": "error", "message": "File exceeds 50MB"}, status=400)
            if not upload_file.name.lower().endswith(".txt"):
                return JsonResponse({"status": "error", "message": "Only .txt files allowed"}, status=400)
            try:
                final_email = upload_file.read().decode("utf-8", errors="ignore").strip()
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"File read error: {str(e)}"}, status=400)
        elif raw_text:
            final_email = raw_text

    if not final_email:
        return JsonResponse({"status": "error", "message": "Paste email/header or upload a .txt file"}, status=400)

    valid, msg = _validate_header_input(final_email)
    if not valid:
        return JsonResponse({"status": "error", "message": msg}, status=400)

    try:
        analyzer = ProfessionalEmailAnalyzer(
            final_email,
            ip_blacklist_checker=ip_blacklists,
            domain_blacklist_checker=domain_blacklists,
        )
        results = analyzer.analyze()
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Analysis error: {str(e)}"}, status=500)

    try:
        deduct_service_credits(user_id, 'header_analysis', 1,
                               ref_type='ip_check', description='Header Analysis')
        # Effective balance, not the raw AC column: after the cutover a user
        # spending from the new wallet would otherwise be told 0 remain while
        # still being able to run analyses.
        remaining_credits = get_effective_balance(user_id, 'header_analysis')
    except InsufficientCredits:
        # Lost a race against another request since the gate above. Withhold
        # the result rather than serve it free.
        return JsonResponse({"status": "error", "message": "No Analysis Credits left. Please upgrade your plan to continue."}, status=429)
    except Exception:
        remaining_credits = None

    try:
        from Email_validate_app.models import EmailHeader
        domain_info = results.get('domain_info') or {}
        raw_ip = results.get('origin_ip', '')
        safe_ip = raw_ip if raw_ip and raw_ip != 'Not Found' else None
        raw_to = results.get('to', '')
        to_match = re.search(r'[\w\.-]+@[\w\.-]+', raw_to or '')
        EmailHeader.objects.create(
            user_id      = user_id,
            raw_header   = final_email,
            origin_ip    = safe_ip,
            from_email   = domain_info.get('from_email') or None,
            to_email     = to_match.group(0) if to_match else None,
            subject      = (results.get('subject') or '')[:255] or None,
            spf_status   = results.get('spf',  'unknown'),
            dkim_status  = results.get('dkim', 'unknown'),
            dmarc_status = results.get('dmarc','unknown'),
            spam_score   = results.get('score', 0),
            risk_level   = results.get('risk', 'SAFE'),
        )
    except Exception:
        pass  # never block the response if persistence fails

    return JsonResponse({
        "status": "ok",
        "credits": remaining_credits,
        "results": results,
    })


# ── Bulk Validation ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
@api_key_required
def api_bulk_validate(request):
    """
    POST /api/validate/bulk/
    Option 1 — CSV file upload: multipart/form-data with field "file"
    Option 2 — JSON body: {"emails": ["a@b.com", "c@d.com"]}
    Returns: {job_id, table_name, total, status, status_url, results_url}
    """
    try:
        user = request.api_key.user
        if not user:
            return JsonResponse({"error": "API key not linked to any user"}, status=403)

        current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)\
                           .astimezone(pytz.timezone("Asia/Kolkata"))\
                           .replace(tzinfo=None)

        # ── Option 1: CSV file upload ─────────────────────────
        if request.FILES.get("file"):
            uploaded_file = request.FILES["file"]
            filename = secure_filename(uploaded_file.name)
            file_ext = os.path.splitext(filename)[1].lower()

            if file_ext not in SUPPORTED_EXTENSIONS:
                return JsonResponse({
                    "error": f"Unsupported format. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
                }, status=400)

            # Save file
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            with open(file_path, 'wb+') as f:
                for chunk in uploaded_file.chunks():
                    f.write(chunk)

            # Read into DataFrame
            df = read_file(file_path, file_ext)
            if df.empty:
                return JsonResponse({"error": "Uploaded file is empty"}, status=400)

        # ── Option 2: JSON email list ─────────────────────────
        elif request.content_type == "application/json":
            try:
                data = json.loads(request.body)
                emails = data.get("emails", [])
            except Exception:
                return JsonResponse({"error": "Invalid JSON body"}, status=400)

            if not emails:
                return JsonResponse({"error": "emails list is required"}, status=400)

            if len(emails) > 1000000:
                return JsonResponse({"error": "Max 10 lakh emails per request"}, status=400)

            df = pd.DataFrame({"email": emails})
            filename = f"api_bulk_{uuid.uuid4().hex[:8]}.csv"
            file_ext = ".csv"
            file_path = os.path.join(UPLOAD_FOLDER, filename)

        else:
            return JsonResponse({"error": "Send CSV file or JSON body with emails list"}, status=400)

        # ── Create ListFiles record ───────────────────────────
        list_file = ListFiles.objects.create(
            file_name=filename,
            user_id=user.id,
            insert_date=current_datetime,
            job_status="Processing",
        )
        file_id = list_file.file_id

        # ── Generate table name ───────────────────────────────
        table_name = f"WIN_{file_id}_{current_datetime.strftime('%Y_%m_%d')}"
        table_name = re.sub(r'\W|^(?=\d)', '_', table_name)

        list_file.table_name = table_name
        list_file.save()

        # ── Detect email column ───────────────────────────────
        if request.content_type == "application/json":
            email_column = "email"  # JSON input always uses "email" key
        else:
            _tmp_csv = os.path.join(UPLOAD_FOLDER, f"{table_name}_tmp.csv")
            df.to_csv(_tmp_csv, index=False)
            email_column = find_emailcolumn_file(_tmp_csv)
            try:
                os.remove(_tmp_csv)
            except Exception:
                pass

        if not email_column:
            list_file.job_status = "Stopped"
            list_file.save()
            return JsonResponse({"error": "No email column found in file"}, status=400)

        # ── Prepare DataFrame ─────────────────────────────────
        df.insert(0, "ivc_id", range(1, len(df) + 1))
        df = df.where(pd.notnull(df), None)

        # ── Bulk insert into AllEmails ────────────────────────
        user_obj = UserTable.objects.get(id=user.id)
        email_col = email_column
        non_email_cols = [c for c in df.columns if c not in ("ivc_id", email_col, RESULT_COLUMN_1, RESULT_COLUMN_2)]

        bulk_objs = []
        for _, row in df.iterrows():
            email_val = str(row[email_col]).strip() if row[email_col] else ""
            if not email_val:
                continue
            extra = {"ivc_id": str(row["ivc_id"])}
            for col in non_email_cols:
                if row[col] is not None:
                    extra[col] = str(row[col])
            bulk_objs.append(AllEmails(
                file_id=file_id,
                user=user_obj,
                table_name=table_name,
                email=email_val,
                extra_data=extra,
            ))

        try:
            AllEmails.objects.bulk_create(bulk_objs, ignore_conflicts=True)
            logger.info(f"[API] AllEmails inserted: {len(bulk_objs)}")
        except Exception as ae:
            logger.warning(f"[API] AllEmails bulk_create failed (non-fatal): {ae}")

        # Save CSV for Celery task reference
        file_path_csv = os.path.join(UPLOAD_FOLDER, f"{table_name}.csv")
        df.to_csv(file_path_csv, index=False)

        # ── Fire Celery task ──────────────────────────────────
        validate_email_list_task.delay(table_name, file_path_csv, email_col)
        logger.info(f"[API] Bulk job started: {table_name} | emails: {len(bulk_objs)}")

        return JsonResponse({
            "job_id":      table_name,
            "total":       len(df),
            "status":      "Processing",
            "status_url":  f"/api/validate/status/{table_name}/",
            "results_url": f"/api/validate/results/{table_name}/",
        }, status=202)

    except Exception as e:
        logger.error(f"[API] Bulk validate error: {e}", exc_info=True)
        return JsonResponse({"error": str(e)}, status=500)


# ── Job Status ────────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
@api_key_required
def api_validate_status(request, job_id):
    try:
        job = ListFiles.objects.get(table_name=job_id)
    except ListFiles.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    total = job.total_count or 0
    done  = (job.valid_count or 0) + (job.invalid_count or 0) + \
            (job.unknown_count or 0) + (job.others_count or 0)

    duration = None
    if job.started_at and job.completed_at:
        seconds = (job.completed_at - job.started_at).total_seconds()
        duration = f"{int(seconds // 60)}m {int(seconds % 60)}s"

    return JsonResponse({
        "job_id":       job_id,
        "status":       job.job_status,
        "total":        total,
        "valid":        job.valid_count or 0,
        "invalid":      job.invalid_count or 0,
        "unknown":      job.unknown_count or 0,
        "others":       job.others_count or 0,
        "valid_pct":    job.valid_percentage or 0,
        "invalid_pct":  job.invalid_percentage or 0,
        "progress_pct": round((done / total * 100), 2) if total else 0,
        "started_at":   job.started_at.strftime("%Y-%m-%d %H:%M:%S") if job.started_at else None,
        "completed_at": job.completed_at.strftime("%Y-%m-%d %H:%M:%S") if job.completed_at else None,
        "duration":     duration,
    })


# ── Job Results ───────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
@api_key_required
def api_validate_results(request, job_id):
    """
    GET /api/validate/results/<job_id>/
    Query params: ?page=1&page_size=100&result=Valid
    Returns paginated results
    """
    try:
        job = ListFiles.objects.get(table_name=job_id)
    except ListFiles.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    qs = AllEmails.objects.filter(file_id=job.file_id)

    result_filter = request.GET.get("result")
    if result_filter:
        qs = qs.filter(validation_results=result_filter)

    page      = int(request.GET.get("page", 1))
    page_size = min(int(request.GET.get("page_size", 100)), 1000)
    total     = qs.count()
    start     = (page - 1) * page_size
    rows      = qs.order_by("id")[start: start + page_size]

    records = [
        {
            "Win_Id":             r.extra_data.get("Win_Id", ""),
            "email":              r.email,
            "validation_results": r.validation_results or "",
            "result_reason":      r.extra_data.get("reason", ""),
        }
        for r in rows
    ]

    return JsonResponse({
        "job_id":      job_id,
        "total":       total,
        "page":        page,
        "page_size":   page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "results":     records,
    })
