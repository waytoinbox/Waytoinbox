from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, FileResponse
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.views.decorators.http import require_POST
from django.db.models import Sum, Max
from django.utils import timezone
from datetime import datetime, timedelta
from io import BytesIO
import hashlib
import secrets
import re
import pytz
import json
import razorpay
import pandas as pd
import tempfile
import logging

from razorpay.errors import BadRequestError, ServerError, SignatureVerificationError
import razorpay.errors as razorpay_errors
from xhtml2pdf import pisa
from django.template.loader import render_to_string

from Email_validate_app.models import (
    UserTable, ListFiles, SubsPayment, Payment,
    CurrentCredits, TotalCredits, UsedCredits, AllEmails,
)
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.mailer import send_payment_success_email

logger = logging.getLogger(__name__)

_WIN_TABLE_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')


def _drop_win_table(table_name: str) -> None:
    """Drop orphaned dynamic WIN_* table after job soft-delete (DB-11). Pattern-validated before execution."""
    if not table_name or not _WIN_TABLE_RE.fullmatch(table_name):
        return
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS `%s`' % table_name)  # nosec: pattern-validated above

from Email_validate_app.services.credit_manager import (
    generate_receipt_id,
    get_current_credit, get_vc_current_credit,
    get_ip_current_credit, get_ac_current_credit,
    update_or_insert_current_credit,
    insert_credits, insert_vc_credits,
    insert_ip_credits, insert_ac_credits,
    insert_cc_credits,
    deduct_vc_credits, deduct_ac_credits, deduct_cc_credits,
    calculate_price, manage_credits,
)


def fetch_user_data(user_id):
    """Fetch user details from the database."""
    return UserTable.objects.filter(id=user_id).first()


def _razorpay_payer_method(payment_details):
    method = payment_details.get("method", "")
    if method == "card":
        card    = payment_details.get("card") or {}
        network = card.get("network", "Card")
        last4   = card.get("last4", "")
        ctype   = card.get("type", "")
        parts   = [p for p in [network, ctype, f"···· {last4}" if last4 else ""] if p]
        return " ".join(parts)
    if method == "upi":
        vpa = payment_details.get("vpa", "")
        if any(x in vpa for x in ("okicici", "okaxis", "okhdfcbank", "oksbi")):
            label = "Google Pay"
        elif any(x in vpa for x in ("ybl", "ibl", "axl")):
            label = "PhonePe"
        elif "paytm" in vpa:
            label = "Paytm"
        else:
            label = "UPI"
        return f"{label} ({vpa})" if vpa else label
    if method == "netbanking":
        bank = payment_details.get("bank", "")
        return f"Net Banking ({bank})" if bank else "Net Banking"
    if method == "wallet":
        wallet = payment_details.get("wallet", "")
        return wallet.capitalize() if wallet else "Wallet"
    if method == "emi":
        return "EMI"
    return method.capitalize() if method else "Razorpay"


def pricing(request):
    user_id = get_user_id(request)
    current_credits = 0
    active_plan = None

    if user_id:
        try:
            current_credits = get_current_credit(user_id)
            # Fetch active subscription plan
            active_plan = SubsPayment.objects.filter(user_id=user_id, plan_status="Active").first()
        except Exception as e:
            logger.error("Error fetching credits: %s", e)
            messages.error(request, "An error occurred while fetching your credits. Please try again later.")
            current_credits = 0
    else:
        current_credits = None  # no session

    return render(request, "i_pricing.html", {"credits": current_credits, "active_plan": active_plan})


def order_payment(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method != "POST":
        if is_ajax:
            return JsonResponse({"status": "error", "message": "POST required"}, status=405)
        messages.error(request, "Invalid request method.")
        return redirect('pricing')

    try:
        # Input validation
        credits = request.POST.get("plan")
        price_ = request.POST.get("price")
        price_per_email = request.POST.get("pricePerEmail")
        currency = request.POST.get("usd-inr")
        timezone_str = request.GET.get('timezone', 'Asia/Kolkata')

        logger.debug("credits=%s price=%s price_per_email=%s currency=%s", credits, price_, price_per_email, currency)

        user_id = get_user_id(request)
        current_credits = get_current_credit(user_id)

        if not credits or not price_:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Credits and price must be provided."}, status=400)
            messages.warning(request, "Credits and price must be provided.")
            return redirect('subscription')

        try:
            credits = int(credits)
            price_ = float(price_)
        except ValueError:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid input for credits or price."}, status=400)
            messages.error(request, "Invalid input for credits or price.")
            return redirect('subscription')

        # Minimum order check
        if price_ < 1.0 or credits < 150:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Order amount must be at least $1.00 and minimum 150 credits."}, status=400)
            messages.error(request, "Order amount must be at least $1.00 and minimum credits: 150.")
            return redirect('subscription')

        user_data = fetch_user_data(user_id)
        if not user_data:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found. Please log in."}, status=404)
            messages.error(request, "User not found. Please log in.")
            return redirect('subscription')

        # Fetch user's subscription plan
        subs_plan = None
        try:
            latest_sub = (
                SubsPayment.objects
                .filter(user_id=user_id, plan_status="active")
                .latest('payment_time')
            )
            subs_plan = latest_sub.subs_plan
        except SubsPayment.DoesNotExist:
            subs_plan = None

        logger.debug("Subscription plan for user %s: %s", user_id, subs_plan)

        # Apply discount by plan
        discount_percentage = 0
        if subs_plan:
            plan = subs_plan.strip().lower()
            if plan == "classic":
                discount_percentage = 2
            elif plan == "standard":
                discount_percentage = 4
            elif plan == "advanced":
                discount_percentage = 8

        discounted_price = round(price_ - (price_ * discount_percentage / 100), 2)
        logger.debug("Price=%s discount=%s%% final=%s", price_, discount_percentage, discounted_price)

        # Razorpay payment integration
        receipt_id = generate_receipt_id(timezone_str)
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        data = {
            "amount": int(discounted_price * 100),  # in paise
            "currency": currency,
            "receipt": receipt_id,
        }

        try:
            payment = client.order.create(data=data)
            payment['display_amount'] = payment['amount'] / 100
        except BadRequestError as e:
            logger.error("Razorpay bad request error: %s", e)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid request to payment gateway."}, status=400)
            messages.error(request, "Invalid request to payment gateway.")
            return redirect('subscription')
        except ServerError as e:
            logger.error("Razorpay server error: %s", e)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Payment gateway server error."}, status=502)
            messages.error(request, "Payment gateway server error.")
            return redirect('subscription')
        except razorpay_errors.RazorpayError as e:
            logger.error("Razorpay error: %s", e)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Payment could not be initiated. Please try again."}, status=500)
            messages.error(request, "Payment could not be completed due to technical issues.")
            return redirect('subscription')

        if is_ajax:
            return JsonResponse({
                "status":              "ok",
                "key_id":              settings.RAZORPAY_KEY_ID,
                "order_id":            payment['id'],
                "amount":              payment['amount'],
                "currency":            payment.get('currency', currency),
                "user_name":           user_data.user_name,
                "user_email":          user_data.user_email,
                "user_id":             user_data.id,
                "credit":              credits,
                "plan":                price_per_email,
                "discount_percentage": discount_percentage,
                "flow":                "payg",
            })

        return render(request, "i_payment_2.html", {
            "credits":             current_credits,
            "payment":             payment,
            "user_data":           user_data,
            "credit":              credits,
            "currency":            currency,
            "plan":                price_per_email,
            "discount_percentage": discount_percentage,
            "discounted_price":    discounted_price,
            "current_credits":     current_credits,
            "key_id":              settings.RAZORPAY_KEY_ID,
        })

    except Exception as e:
        logger.error("Unexpected error in payment init: %s", e, exc_info=True)
        if is_ajax:
            return JsonResponse({"status": "error", "message": "An unexpected error occurred."}, status=500)
        messages.error(request, "An unexpected error occurred.")
        return redirect('subscription')


def payment(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        # SEC-01: always resolve user from session — never trust client-supplied user_id
        user_id = get_user_id(request)
        if not user_id:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Not authenticated."}, status=401)
            messages.error(request, "Not authenticated.")
            return redirect('login')

        payment_id         = request.POST.get('payment_id')
        order_id           = request.POST.get('order_id')
        razorpay_signature = request.POST.get('razorpay_signature', '')
        credits            = request.POST.get('credits')
        currency           = request.POST.get('currency')
        plans_val          = request.POST.get('plan')
        description        = request.POST.get('description')
        payer_name         = request.POST.get('user_name')

        # SEC-02: verify Razorpay payment signature before crediting
        if payment_id and order_id and razorpay_signature:
            try:
                client_verify = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
                client_verify.utility.verify_payment_signature({
                    'razorpay_order_id':   order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature':  razorpay_signature,
                })
            except SignatureVerificationError:
                logger.warning("Payment signature verification failed: order=%s payment=%s user=%s", order_id, payment_id, user_id)
                if is_ajax:
                    return JsonResponse({"status": "error", "message": "Payment verification failed."}, status=400)
                messages.error(request, "Payment verification failed.")
                return redirect('pricing')
        else:
            logger.warning("Missing payment signature: order=%s payment=%s user=%s", order_id, payment_id, user_id)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid payment data."}, status=400)
            messages.error(request, "Invalid payment data.")
            return redirect('pricing')

        try:
            user = UserTable.objects.get(id=user_id)
        except UserTable.DoesNotExist:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)
            messages.error(request, "User not found.")
            return redirect('pricing')

        # DB-08: idempotency guard — prevent double-crediting on retry or double-click
        from Email_validate_app.models import Payment as _Payment
        if _Payment.objects.filter(order_id=order_id).exists():
            logger.warning("Duplicate payment attempt blocked: order=%s user=%s", order_id, user_id)
            if is_ajax:
                return JsonResponse({"status": "ok", "message": "Payment already processed."})
            messages.info(request, "This payment was already processed.")
            return redirect('pricing')

        # Initialize Razorpay client
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        try:
            # Fetch payment details from Razorpay
            payment_details = client.payment.fetch(payment_id)

            if payment_details:
                payer_email = payment_details.get("email")
                customer_contact = payment_details.get("contact")
                amount = f"{int(payment_details.get('amount', 0)) / 100:.2f}"
            else:
                payer_email = request.POST.get('user_email')
                customer_contact = request.POST.get('user_contact')
                amount = request.POST.get('amount')
                if amount and amount.isdigit() and int(amount) > 1000:
                    amount = f"{int(amount) / 100:.2f}"

            # Calculate current time for payment
            current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)
            payment_time = current_datetime

            # Create Payment record in the database
            from django.db import IntegrityError as _IntegrityError
            try:
                payment_obj = Payment(
                    user=user,
                    order_id=order_id,
                    payment_id=payment_id,
                    payer_id=payment_id,
                    payer_name=payer_name,
                    payer_email=payer_email,
                    payer_address=customer_contact,
                    payer_method=_razorpay_payer_method(payment_details) if payment_details else "Razorpay",
                    unit_price=plans_val,
                    amount=amount,
                    currency=currency,
                    credits=credits,
                    payment_time=payment_time,
                    description=description,
                )
                payment_obj.save()
            except _IntegrityError:
                # DB-08b: concurrent retry raced past the exists() check; the
                # unique constraint on order_id caught it — treat as already-processed.
                logger.warning("Concurrent payment race caught by unique constraint: order=%s", order_id)
                if is_ajax:
                    return JsonResponse({"status": "ok", "message": "Payment already processed."})
                messages.info(request, "This payment was already processed.")
                return redirect('pricing')

            # Insert credits via custom function (if necessary)
            insert_credits(request, user_id, credits)

            if getattr(user, 'notify_payment', True):
                send_payment_success_email(
                    user_name=payer_name,
                    user_email=user.user_email,
                    amount=amount,
                    currency=currency,
                    order_id=order_id,
                    payment_time=payment_time,
                    extra={'type': 'payg', 'credits': int(credits)},
                )
            from Email_validate_app.utils import create_notification
            create_notification(user_id, 'payment',
                f"Payment of {currency} {amount} received — {int(credits)} email credits added",
                url='/Receipt/')

            if is_ajax:
                return JsonResponse({"status": "ok"})
            messages.success(request, f"Payment of {amount} {currency} executed successfully for order {order_id}.")
            return redirect('pricing')
        except razorpay.errors.RazorpayError as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": f"Payment error: {str(e)}"}, status=400)
            messages.error(request, f"Payment error: {str(e)}")
            return redirect('pricing')
        except Exception as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)
            messages.error(request, f"An unexpected error occurred: {str(e)}")
            return redirect('pricing')

    if is_ajax:
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)
    return redirect('pricing')


def download_results(request):
    if request.method == "POST":
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        sld_option = request.POST.get('result')
        tablename = request.POST.get('table_name')
        filename = request.POST.get('file_name')
        user_id = get_user_id(request)
        timezone_str = request.POST.get('timezone')

        results = manage_credits(sld_option, tablename, user_id, timezone_str)
        logger.debug("manage_credits result: %s", results)

        if isinstance(results, str):
            if not results.isdigit():
                return JsonResponse({"status": "error", "message": results}, status=500)
            current_credits = get_current_credit(user_id)
            need_c = int(results) - current_credits
            if need_c:
                minimum_credits = 150
                if need_c < minimum_credits:
                    need_c += 150

            result = calculate_price(need_c)
            if not result[0]:
                return JsonResponse({"status": "error", "message": str(result[1])}, status=400)

            price, plan_value = result[1]
            try:
                user_data = UserTable.objects.get(id=user_id)
            except UserTable.DoesNotExist:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)

            receipt_id = generate_receipt_id("Asia/Kolkata")
            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            try:
                payment_rz = client.order.create(data={
                    "amount": int(price * 100),
                    "currency": "USD",
                    "receipt": receipt_id,
                })
            except razorpay.errors.BadRequestError as e:
                return JsonResponse({"status": "error", "message": "Invalid request to payment gateway."}, status=400)
            except razorpay.errors.RazorpayError as e:
                return JsonResponse({"status": "error", "message": "Payment gateway error."}, status=502)

            return JsonResponse({
                "status":    "need_credits",
                "key_id":    settings.RAZORPAY_KEY_ID,
                "order_id":  payment_rz['id'],
                "amount":    payment_rz['amount'],
                "currency":  payment_rz.get('currency', 'USD'),
                "user_name": user_data.user_name,
                "user_email": user_data.user_email,
                "user_id":   user_data.id,
                "credit":    need_c,
                "plan":      f"{plan_value:.4f}",
                "flow":      "payg",
                "need":      need_c,
                "current":   current_credits,
            })

        elif isinstance(results, list):
            df = pd.DataFrame(results)
            df.drop(columns=['result_reasons'], errors='ignore', inplace=True)
            with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as temp_file:
                df.to_csv(temp_file.name, index=False)
            return FileResponse(open(temp_file.name, 'rb'), as_attachment=True, filename=f"{tablename}_{sld_option}.csv")

        if is_ajax:
            return JsonResponse({"status": "error", "message": "Unexpected result. Please contact support."}, status=500)
        messages.error(request, "Unexpected result format received. Please contact support.")
        return redirect('service')


@require_POST
def delete_query(request):
    """
    Handle file deletion with confirmation.
    User must type 'delete' to confirm.
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        user_id = get_user_id(request)
        if not user_id:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Not authenticated"}, status=401)
            messages.error(request, "User not authenticated.")
            return redirect('services')

        table_name = request.POST.get('table_name_')
        file_name = request.POST.get('file_name')

        if not table_name or not file_name:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Missing required parameters"}, status=400)
            messages.error(request, "Missing required parameters.")
            return redirect('services')

        # Check if file belongs to the logged-in user
        file_record = ListFiles.objects.filter(table_name=table_name, user_id=user_id).first()
        if not file_record:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "File not found or no permission"}, status=404)
            messages.error(request, "File not found or you don't have permission to delete it.")
            return redirect('services')

        # Soft-delete the ListFiles record then drop the orphaned WIN_* table (DB-11)
        file_record.job_status = "Deleted"
        file_record.save()
        _drop_win_table(file_record.table_name)

        if is_ajax:
            return JsonResponse({"status": "ok", "message": f"File '{file_name}' has been deleted."})
        messages.success(request, f"File '{file_name}' has been successfully Deleted.")

    except Exception as e:
        logger.error("Error deleting file: %s", e)
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
        messages.error(request, f"An error occurred while deleting the file: {str(e)}")

    return redirect('services')


def receipt_list(request):
    """
    Display all Payment and Subscription Payment records for the logged-in user, most recent first.
    """
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    # Fetch current credits (your existing util)
    current_credits = get_current_credit(user_id)

    # Get all normal payments
    payments = Payment.objects.filter(user_id=user_id, is_hidden=False).order_by('-id')

    # Get all subscription payments
    subs_payments = SubsPayment.objects.filter(user_id=user_id, is_hidden=False).order_by('-id')

    # User display name
    user = UserTable.objects.filter(id=user_id).first()
    name = user.user_name if user else ''

    return render(request, "i_billing.html", {
        'Receipt': payments,
        'SubsReceipt': subs_payments,
        'credits': current_credits,
        'name': name,
    })


@require_POST
def hide_billing_row(request):
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Login required"}, status=401)
    record_id   = request.POST.get("record_id")
    record_type = request.POST.get("record_type")
    if not record_id or record_type not in ("payg", "subs"):
        return JsonResponse({"status": "error", "message": "Invalid parameters"}, status=400)
    if record_type == "payg":
        updated = Payment.objects.filter(id=record_id, user_id=user_id).update(is_hidden=True)
    else:
        updated = SubsPayment.objects.filter(id=record_id, user_id=user_id).update(is_hidden=True)
    if updated:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "error", "message": "Record not found"}, status=404)


def preview(request, id):
    """
    Render the payment receipt HTML in-browser for preview.
    """
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    payment_data = Payment.objects.filter(id=id, user_id=user_id)
    return render(request, 'i_invoice.html', {'payment_data': payment_data})


def generate_pdf(request, id):
    user_id = get_user_id(request)

    if not user_id:
        return redirect("login")

    def _s(val, default="N/A"):
        return str(val) if val else default

    payment_obj = Payment.objects.filter(id=str(id), user_id=user_id).first()

    if payment_obj:
        invoice_type = "payg"
        payment = {
            "order_id":     _s(payment_obj.order_id),
            "payer_name":   _s(payment_obj.payer_name),
            "payer_email":  _s(payment_obj.payer_email),
            "payer_address": _s(payment_obj.payer_address),
            "description":  _s(payment_obj.description, "Payment"),
            "credits":      _s(payment_obj.credits, "0"),
            "unit_price":   _s(payment_obj.unit_price, "0"),
            "amount":       _s(payment_obj.amount, "0"),
            "payment_time": _s(payment_obj.payment_time),
        }
    else:
        # Fall back to subscription payment
        subs_obj = SubsPayment.objects.filter(id=str(id), user_id=user_id).first()
        if not subs_obj:
            return HttpResponse("Invoice not found")
        invoice_type = "subscription"
        payment = {
            "order_id":     _s(subs_obj.order_id),
            "payer_name":   _s(subs_obj.payer_name),
            "payer_email":  _s(subs_obj.payer_email),
            "payer_address": _s(subs_obj.payer_address),
            "description":  _s(subs_obj.description, "Subscription Plan: " + _s(subs_obj.subs_plan)),
            "credits":      "1",
            "unit_price":   _s(subs_obj.subs_plan, "N/A"),
            "amount":       _s(subs_obj.amount, "0"),
            "payment_time": _s(subs_obj.payment_time),
        }

    html = render_to_string(
        "i_invoice.html",
        {
            "payment": payment,
            "invoice_type": invoice_type,
        }
    )

    result = BytesIO()

    try:
        pdf = pisa.CreatePDF(
            src=html,
            dest=result,
            encoding="UTF-8"
        )

        if pdf.err:
            return HttpResponse("PDF generation failed: " + str(pdf.err))

        result.seek(0)
        response = HttpResponse(
            result.getvalue(),
            content_type="application/pdf"
        )

        response["Content-Disposition"] = 'attachment; filename="invoice.pdf"'
        return response

    except Exception as e:
        import traceback
        error_message = f"PDF generation error: {str(e)}\n{traceback.format_exc()}"
        return HttpResponse(error_message, status=500)


@require_POST
def contact_us(request):
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Login required"}, status=401)

    from django.core.mail import send_mail

    user = fetch_user_data(user_id)
    user_name = user.user_name
    user_email = user.user_email

    name = request.POST.get('name')
    email = request.POST.get('email')
    message = request.POST.get('message')

    subject = f"Customer Support Request from {name}"
    full_message = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"User Id: {user_id}\n"
        f"User Name: {user_name}\n"
        f"User Email: {user_email}\n"
        f"Message: {message}"
    )

    try:
        send_mail(
            subject,
            full_message,
            'support@waytoinbox.com',
            ['waytoinbox.notification@gmail.com'],
            fail_silently=False
        )
        return JsonResponse({"status": "success", "message": "Thank you! We'll get back to you within 24 hours."})
    except Exception as e:
        return JsonResponse({"status": "error", "message": "Failed to send message. Please try again."}, status=500)


# add_ip_credit_view was removed: it was routed publicly at
# /add_ip_credit_view/ with no authentication check, hardcoded user_id = 7,
# and granted 50 AC credits on every request — so anyone could mint unlimited
# credits for that account by hitting the URL repeatedly. It was leftover
# scaffolding ("Replace with actual user ID logic"), not a used feature.
# Admin credit grants belong in the admin console, audited via CreditAuditLog.
