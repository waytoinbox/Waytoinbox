import os
import re
import sys
import subprocess
import threading
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from datetime import datetime

import pytz
import razorpay

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.core.paginator import Paginator
from django.db import connection, close_old_connections
from django.db.models import Sum

from Email_validate_app.services.filter_utils import extract_filter_params, apply_search, apply_date_range
from Email_validate_app.services.filter_status import EMAIL_VALIDATE_STATUSES

from Email_validate_app.models import (
    ListFiles, UserTable, CurrentCredits, UsedCredits,
    EmailValidate, EmailValidationLog,
)
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.email_validation import (
    validate_emails_in_parallel, find_email_column, can_validate_email,
)
from Email_validate_app.tasks.verify_emails import (
    create_job, validate_email_list_task, find_emailcolumn_file,
)
from Email_validate_app.services.api_auth import api_key_required

from .billing import get_current_credit, calculate_price, generate_receipt_id
from Email_validate_app.services.credit_manager import deduct_vc_credits
from Email_validate_app.services.email_validation import core_validate_email

# DB-01: strict allowlist — table names are always WIN_<id>_<YYYY>_<MM>_<DD>
_TABLE_NAME_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')


def _require_owned_table(table, user_id):
    """Validate pattern (DB-01) and ownership (DB-02). Returns ListFiles or raises."""
    if not _TABLE_NAME_RE.fullmatch(table or ''):
        raise ValueError("Invalid table name.")
    try:
        return ListFiles.objects.get(table_name=table, user_id=user_id)
    except ListFiles.DoesNotExist:
        raise PermissionError("Access denied.")

UPLOAD_FOLDER = str(settings.PRIVATE_UPLOAD_ROOT)  # INF-11

logger = logging.getLogger(__name__)


def service_validate_emails(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        if not request.session.get('logged_in'):
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Login required"}, status=401)
            messages.warning(request, "You need to log in to access this service.")
            return redirect(reverse('login'))

        if request.method == 'POST':
            file = request.FILES.get('file_')

            if not file:
                logging.warning("Upload attempted but no file found in request.FILES")
                if is_ajax:
                    return JsonResponse({"status": "error", "message": "No file uploaded. Please select a file."}, status=400)
                messages.error(request, "No file uploaded! Please upload a file to proceed.")
                return redirect(reverse("services"))

            try:
                sanitized_table_name, file_id, mess = create_job(file, request)

                if mess == "Job Created":
                    file_record = ListFiles.objects.filter(pk=file_id).first()
                    if file_record:
                        file_record.table_name = sanitized_table_name
                        file_record.save()
                        logging.info(f"Updated table_name for file_id {file_id}: {sanitized_table_name}")
                    else:
                        logging.error(f"No record found with file_id {file_id}")
                else:
                    if is_ajax:
                        return JsonResponse({"status": "error", "message": f"File upload failed: {mess}"}, status=400)
                    messages.error(request, f"File upload failed: {mess}")
                    return redirect(reverse("services"))

                if is_ajax:
                    return JsonResponse({"status": "ok"})
                return redirect(reverse("services"))

            except Exception as file_error:
                logging.error(f"An error occurred during file validation: {file_error}")
                if is_ajax:
                    return JsonResponse({"status": "error", "message": str(file_error)}, status=500)
                messages.error(request, f"An error occurred: {str(file_error)}")
                return redirect(reverse("services"))

        if is_ajax:
            return JsonResponse({"status": "error", "message": "POST required"}, status=405)
        messages.error(request, "Invalid request method.")
        return redirect(reverse("services"))

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
        messages.error(request, "An unexpected error occurred. Please try again later.")
        return redirect(reverse("services"))


def run_email_validation(table, file_path, email_column):
    # Full path for Python executable in the virtual environment
    python_exec = sys.executable
    manage_py = os.path.join(settings.BASE_DIR, 'manage.py')

    try:
        result = subprocess.run(
            [python_exec, manage_py, "validate_emails", table, file_path, email_column],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

    except subprocess.CalledProcessError as e:
        logging.error(f"Subprocess failed: {e.stderr}")
        # Handle error gracefully if needed


def run_analysis_in_background(table, column_name):
    try:
        # Belt-and-suspenders: reject invalid names even in the background thread
        if not _TABLE_NAME_RE.fullmatch(table or ''):
            logger.error("run_analysis_in_background: rejected invalid table %r", table)
            return

        close_old_connections()

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT `{column_name}` FROM `{table}`")
            rows = cursor.fetchall()
            data = [{column_name: row[0]} for row in rows]

        results, invalid_percentage = validate_emails_in_parallel(data, column_name)

        # Re-fetch and update inside thread-safe context
        close_old_connections()
        list_file = ListFiles.objects.filter(table_name=table).first()
        if list_file:
            list_file.free_analyze = invalid_percentage
            list_file.save()
            logger.info("Analysis complete. Updated free_analyze to %s", invalid_percentage)
        else:
            logger.warning("ListFiles entry not found to update for table %s", table)

    except Exception as e:
        logger.error("Background task error: %s", e)
        try:
            close_old_connections()
            ListFiles.objects.filter(table_name=table).update(free_analyze=-2)
        except Exception:
            pass
    finally:
        close_old_connections()


def Analyze(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # DB-02: require authentication
    if not request.session.get('logged_in'):
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Login required"}, status=401)
        return redirect(reverse('login'))

    user_id = get_user_id(request)
    table = request.GET.get("table_name")
    if not table:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No table name provided."}, status=400)
        return HttpResponse("No table name provided.", status=400)

    # DB-01 + DB-02: validate pattern and ownership in one step
    try:
        list_file = _require_owned_table(table, user_id)
    except (ValueError, PermissionError) as e:
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=403)
        return HttpResponse(str(e), status=403)

    list_file.free_analyze = -1
    list_file.save()

    column = find_email_column(table)
    if not column:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No email column found."}, status=400)
        return HttpResponse("No email column found.", status=400)

    column_name = column[0]

    if column_name != "_Emails_":
        with connection.cursor() as cursor:
            cursor.execute(
                f"ALTER TABLE `{table}` RENAME COLUMN `{column_name}` TO `_Emails_`"
            )
        column_name = "_Emails_"

    thread = Thread(target=run_analysis_in_background, args=(table, column_name))
    thread.start()

    if is_ajax:
        return JsonResponse({"status": "ok"})
    return redirect(reverse("services"))


def single_service(request):
    import ast
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    page_obj = None
    current_credits = 0
    name = None

    try:
        user = UserTable.objects.get(id=user_id)

        current_credits_obj = CurrentCredits.objects.filter(user=user).first()
        current_credits = current_credits_obj.vc_current_credits if current_credits_obj else 0
        name = user.user_name

    except UserTable.DoesNotExist:
        return redirect('login')
    except Exception as e:
        logger.error("Error fetching user: %s", e)

    f = extract_filter_params(request)
    PAGE_SIZE = getattr(settings, 'FILTER_DEFAULT_PAGE_SIZE', 25)

    try:
        qs = EmailValidate.objects.filter(user_id=user_id, is_hidden=False).order_by('-insert_date')
        qs = apply_search(qs, f['search'], 'email', 'mx_record')
        if f['status']:
            qs = qs.filter(mx_found__icontains=f['status'])
        qs = apply_date_range(qs, f['date_from'], f['date_to'], field='insert_date')

        paginator = Paginator(qs, PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        for item in page_obj.object_list:
            if item.mx_found:
                try:
                    item.mx_found = ast.literal_eval(item.mx_found)
                except (ValueError, SyntaxError):
                    item.mx_found = (item.mx_found, "")

    except Exception as e:
        logger.error("Error fetching email history: %s", e)

    context = {
        'email': request.session.get("logged_in"),
        'page_obj': page_obj,
        'name': name,
        'credits': current_credits,
        'search': f['search'],
        'status': f['status'],
        'date_from': str(f['date_from']) if f['date_from'] else '',
        'date_to': str(f['date_to']) if f['date_to'] else '',
        'pf_statuses': EMAIL_VALIDATE_STATUSES,
        'pf_show_status': True,
        'pf_search_placeholder': 'Search by email or MX record…',
    }

    return render(request, "i_email_verify.html", context)


@require_POST
def hide_email_history(request):
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Login required"}, status=401)
    record_id = request.POST.get("record_id")
    if not record_id:
        return JsonResponse({"status": "error", "message": "Missing record_id"}, status=400)
    updated = EmailValidate.objects.filter(id=record_id, user_id=user_id).update(is_hidden=True)
    if updated:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "error", "message": "Record not found"}, status=404)


def single_verify(request):
    if not get_user_id(request):
        return JsonResponse({"status": "error", "message": "Login required"}, status=401)

    user_id = get_user_id(request)

    if request.method == "POST":
        email = request.POST.get('_email_')
        if not email:
            return JsonResponse({"status": "error", "message": "Please enter an email."})

        try:
            current_credits = get_current_credit(user_id)
        except Exception:
            current_credits = 0

        if current_credits < 1:
            if not can_validate_email(user_id):
                return JsonResponse({"status": "error", "message": "Daily free limit reached. Max 5 emails per day."})
            email_result = core_validate_email(user_id, email, deduct_credits=False)
        else:
            email_result = core_validate_email(user_id, email, deduct_credits=True)

        if email_result.get("error"):
            return JsonResponse({"status": "error", "message": "Validation error: " + str(email_result['error'])})

        return JsonResponse({
            "status":    "ok",
            "email":     email_result["email"],
            "result":    str(email_result["result"]),
            "reason":    str(email_result.get("reason", "") or ""),
            "mx_record": str(email_result["mx_record"]),
        })

    return JsonResponse({"status": "error", "message": "Invalid request"})


def verify_emails(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # DB-02: require authentication before anything else
    if not request.session.get('logged_in'):
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Login required"}, status=401)
        return redirect(reverse('login'))

    uid = get_user_id(request)
    table = request.GET.get("table_name")

    if not table:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Missing table_name"}, status=400)
        return HttpResponseBadRequest("Missing table_name parameter")

    # DB-01 + DB-02: validate pattern and ownership
    try:
        _require_owned_table(table, uid)
    except (ValueError, PermissionError) as e:
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=403)
        return HttpResponse(str(e), status=403)

    try:
        file_id = int(table.split("_")[1])
    except (IndexError, ValueError):
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Invalid table name format"}, status=400)
        return HttpResponseBadRequest("Invalid table name format")

    # DB-03: scope the update to the current user's file
    ListFiles.objects.filter(file_id=file_id, user_id=uid).update(total_count=0)

    file_name = f"{table}.csv"
    file_path = os.path.join(UPLOAD_FOLDER, file_name)

    if not os.path.isfile(file_path):
        if is_ajax:
            return JsonResponse({"status": "error", "message": f"File {file_name} not found."}, status=404)
        messages.error(request, f"File {file_name} not found at {file_path}.")
        return redirect("services")

    email_column = find_emailcolumn_file(file_path)
    if not email_column:
        # DB-03: scope status update to the current user's file
        ListFiles.objects.filter(table_name=table, user_id=uid).update(job_status="Stopped")
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No email column found."}, status=400)
        messages.error(request, "No email column found.")
        return redirect("services")

    # Count rows to check credits before validation starts
    try:
        with open(file_path, newline="", encoding="utf-8-sig") as _f:
            total_rows = sum(1 for _ in _f) - 1  # subtract header
    except Exception:
        total_rows = 0

    # uid already set at top of view (auth check)
    current_credits = get_current_credit(uid)

    if total_rows > current_credits:
        need_c = total_rows - current_credits
        if need_c < 150:
            need_c += 150
        result = calculate_price(need_c)
        if not result[0]:
            if is_ajax:
                return JsonResponse({"status": "error", "message": str(result[1])}, status=400)
            messages.error(request, str(result[1]))
            return redirect("services")
        price, plan_value = result[1]
        try:
            user_data = UserTable.objects.get(id=uid)
        except UserTable.DoesNotExist:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)
            return redirect("services")
        receipt_id = generate_receipt_id("Asia/Kolkata")
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        payment = client.order.create(data={"amount": int(price * 100), "currency": "USD", "receipt": receipt_id})
        return JsonResponse({
            "status":     "need_credits",
            "key_id":     settings.RAZORPAY_KEY_ID,
            "order_id":   payment['id'],
            "amount":     payment['amount'],
            "currency":   payment.get('currency', 'USD'),
            "user_name":  user_data.user_name,
            "user_email": user_data.user_email,
            # DB-03: user_id removed — backend derives it from session, never from client
            "credit":     need_c,
            "plan":       f"{plan_value:.4f}",
            "flow":       "payg",
            "need":       need_c,
            "current":    current_credits,
        })

    # Enough credits — deduct before validation starts
    deduct_vc_credits(uid, total_rows, ref_type='validation', description=f"Bulk validation: {table}")
    ListFiles.objects.filter(table_name=table, user_id=uid).update(credite_status="Credited")

    # Fetch user details for completion notification
    notify_email = ""
    notify_name = ""
    try:
        if uid:
            u = UserTable.objects.get(id=uid)
            notify_email = u.user_email or ""
            notify_name = u.user_name or ""
    except Exception:
        pass

    # Dispatch to Celery
    validate_email_list_task.delay(table, file_path, email_column, notify_email, notify_name)

    ListFiles.objects.filter(table_name=table, user_id=uid).update(job_status="Processing")
    if is_ajax:
        return JsonResponse({"status": "ok"})
    return redirect("services")


@csrf_exempt
@require_http_methods(["POST"])
@api_key_required
def api_single_validate(request):
    import json
    try:
        data = json.loads(request.body)
        email = data.get("email", "").strip()
    except Exception:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    if not email:
        return JsonResponse({"error": "email field is required"}, status=400)

    # Get user from API key
    user = request.api_key.user
    if not user:
        return JsonResponse({"error": "API key not linked to any user"}, status=403)

    # Use same core function as website
    email_result = core_validate_email(user.id, email, deduct_credits=False)

    if email_result.get("error"):
        return JsonResponse({"error": email_result["error"]}, status=500)

    return JsonResponse({
        "email":     email_result["email"],
        "result":    email_result["result"],
        "reason":    email_result["reason"],
        "mx_record": email_result["mx_record"],
    })
