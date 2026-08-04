import logging

from django.shortcuts import render, redirect
from django.http import JsonResponse
from razorpay.errors import SignatureVerificationError
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
import pytz
import razorpay

from Email_validate_app.models import UserTable, SubsPayment
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.mailer import send_payment_success_email

from .billing import (
    get_ac_current_credit,
    generate_receipt_id,
    _razorpay_payer_method,
    insert_vc_credits,
    insert_ac_credits,
    insert_cc_credits,
)


def subscription(request):
    user_id = get_user_id(request)

    try:
        ip_current_credits = get_ac_current_credit(user_id)
    except Exception as e:
        logger.error("Error fetching credits: %s", e)
        ip_current_credits = 0

    active_plan = None
    try:
        sub = SubsPayment.objects.filter(user_id=user_id, plan_status="Active").latest('payment_time')

        if sub.valid_time and sub.valid_time >= timezone.now():
            active_plan = {
                "subs_plan": sub.subs_plan,
                "valid_time": sub.valid_time,
            }
        else:
            sub.plan_status = "expired"
            sub.save(update_fields=["plan_status"])

    except SubsPayment.DoesNotExist:
        active_plan = None

    return render(request, "i_subscription.html", {
        "credits": ip_current_credits,
        "active_plan": active_plan,
        "subs_plan": active_plan["subs_plan"] if active_plan else None,
    })


def subscription_success(request):
    if request.method == "POST":
        try:
            import json as _json
            data = _json.loads(request.body)
            return JsonResponse({"status": "ok"})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
    return JsonResponse({"error": "Invalid method"}, status=405)


def subscription_cancel(request):
    return redirect(reverse('subscription'))


def create_subscription(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        subs_plan     = request.POST.get('plan')
        subs_price    = request.POST.get('price')
        billing_cycle = request.POST.get('billing_cycle', 'monthly')
        contacts      = request.POST.get('contacts', '0')

        if not subs_plan or not subs_price:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Missing required fields."}, status=400)
            from django.http import HttpResponseBadRequest
            return HttpResponseBadRequest("Missing required fields.")

        user_id = get_user_id(request)
        try:
            user = UserTable.objects.get(id=user_id)
        except UserTable.DoesNotExist:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found. Please log in."}, status=404)
            messages.error(request, "User not found.")
            return redirect('subscription')

        ip_current_credits = 0
        if user_id:
            try:
                ip_current_credits = get_ac_current_credit(user_id)
            except Exception as e:
                logger.error("Error fetching credits: %s", e)
                ip_current_credits = 0
        else:
            ip_current_credits = None

        receipt_id = generate_receipt_id("Asia/Kolkata")
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        data = {
            "amount": int(float(subs_price) * 100),
            "currency": "USD",
            "receipt": receipt_id,
        }

        try:
            payment_rz = client.order.create(data=data)
            payment_rz['display_amount'] = payment_rz['amount'] / 100
        except razorpay.errors.BadRequestError as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid request to payment gateway."}, status=400)
            messages.error(request, "Invalid request to payment gateway.")
            return redirect('subscription')
        except razorpay.errors.ServerError as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Payment gateway server error."}, status=502)
            messages.error(request, "Payment gateway server error.")
            return redirect('subscription')
        except Exception as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Payment could not be initiated. Please try again."}, status=500)
            messages.error(request, "Payment could not be completed due to technical issues.")
            return redirect('subscription')

        if is_ajax:
            return JsonResponse({
                "status":        "ok",
                "key_id":        settings.RAZORPAY_KEY_ID,
                "order_id":      payment_rz['id'],
                "amount":        payment_rz['amount'],
                "currency":      payment_rz.get('currency', 'INR'),
                "user_name":     user.user_name,
                "user_email":    user.user_email,
                "user_id":       user.id,
                "credit":        subs_plan,
                "flow":          "subscription",
                "billing_cycle": billing_cycle,
                "contacts":      contacts,
            })

        return render(request, "i_payment.html", {
            "key_id":    settings.RAZORPAY_KEY_ID,
            "user_data": user,
            "payment":   payment_rz,
            "credit":    subs_plan,
            "currency":  'USD',
            "credits":   ip_current_credits,
        })

    if is_ajax:
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)
    from django.http import HttpResponseBadRequest
    return HttpResponseBadRequest("Invalid request method.")


def subs_payment(request):
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
        plans_val          = request.POST.get('credits')
        currency           = request.POST.get('currency')
        description        = request.POST.get('description')
        payer_name         = request.POST.get('user_name')
        cc_credits_count   = int(request.POST.get('contacts', 0) or 0)

        # SEC-02: verify Razorpay signature before activating subscription
        if payment_id and order_id and razorpay_signature:
            try:
                import razorpay as _rz
                _client = _rz.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
                _client.utility.verify_payment_signature({
                    'razorpay_order_id':   order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature':  razorpay_signature,
                })
            except SignatureVerificationError:
                logger.warning("Subs payment signature failed: order=%s payment=%s user=%s", order_id, payment_id, user_id)
                if is_ajax:
                    return JsonResponse({"status": "error", "message": "Payment verification failed."}, status=400)
                messages.error(request, "Payment verification failed.")
                return redirect('subscription')
        else:
            logger.warning("Missing subs signature: order=%s payment=%s user=%s", order_id, payment_id, user_id)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid payment data."}, status=400)
            messages.error(request, "Invalid payment data.")
            return redirect('subscription')

        if plans_val == "Classic":
            vc_credits = 1050
            ac_credits = 5
        elif plans_val == "Standard":
            vc_credits = 5100
            ac_credits = 10
        else:
            vc_credits = 10500
            ac_credits = 30

        try:
            user = UserTable.objects.get(id=user_id)
        except UserTable.DoesNotExist:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)
            messages.error(request, "User not found.")
            return redirect('subscription')

        # DB-08: idempotency guard — prevent double-crediting on retry or double-click
        from Email_validate_app.models import SubsPayment as _SubsPayment
        if _SubsPayment.objects.filter(order_id=order_id).exists():
            logger.warning("Duplicate subs payment attempt blocked: order=%s user=%s", order_id, user_id)
            if is_ajax:
                return JsonResponse({"status": "ok", "message": "Payment already processed."})
            messages.info(request, "This payment was already processed.")
            return redirect('subscription')

        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        try:
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

            billing_cycle = request.POST.get('billing_cycle', 'monthly')
            current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)
            payment_time = current_datetime
            valid_time = payment_time + timedelta(days=365 if billing_cycle == 'yearly' else 30)

            SubsPayment.objects.filter(user=user, plan_status="Active").update(plan_status="Inactive")

            from django.db import IntegrityError as _IntegrityError
            try:
                payment_obj = SubsPayment(
                    user=user,
                    order_id=order_id,
                    payment_id=payment_id,
                    payer_id=payment_id,
                    payer_name=payer_name,
                    payer_email=payer_email,
                    payer_address=customer_contact,
                    payer_method=_razorpay_payer_method(payment_details) if payment_details else "Razorpay",
                    subs_plan=plans_val,
                    plan_status="Active",
                    amount=amount,
                    currency=currency,
                    vc_credits=str(vc_credits),
                    ac_credits=str(ac_credits),
                    cc_credits=str(cc_credits_count),
                    billing_cycle=billing_cycle,
                    payment_time=payment_time,
                    valid_time=valid_time,
                    description=description,
                )
                payment_obj.save()
            except _IntegrityError:
                # DB-08b: concurrent retry raced past the exists() check; unique
                # constraint on order_id caught it — treat as already-processed.
                logger.warning("Concurrent subs payment race caught by unique constraint: order=%s", order_id)
                if is_ajax:
                    return JsonResponse({"status": "ok", "message": "Payment already processed."})
                messages.info(request, "This payment was already processed.")
                return redirect('subscription')

            insert_vc_credits(request, user_id, vc_credits, ref_type='subscription', ref_id=order_id)
            insert_ac_credits(request, user_id, ac_credits, ref_type='subscription', ref_id=order_id)
            if cc_credits_count > 0:
                insert_cc_credits(request, user_id, cc_credits_count, ref_type='subscription', ref_id=order_id)

            if getattr(user, 'notify_payment', True):
                send_payment_success_email(
                    user_name=payer_name,
                    user_email=user.user_email,
                    amount=amount,
                    currency=currency,
                    order_id=order_id,
                    payment_time=payment_time,
                    extra={
                        'type': 'subscription',
                        'plan': plans_val,
                        'vc_credits': vc_credits,
                        'ac_credits': ac_credits,
                        'valid_till': valid_time.strftime('%d %b %Y') if valid_time else 'N/A',
                    },
                )
            from Email_validate_app.utils import create_notification
            create_notification(user_id, 'payment',
                f"Payment of {currency} {amount} received — {plans_val} plan activated",
                url='/subscription/')

            if is_ajax:
                return JsonResponse({"status": "ok", "plan": plans_val})
            messages.success(request, f"Payment of {amount} {currency} executed successfully for order {order_id}. New plan activated.")
            return redirect('subscription')

        except razorpay.errors.RazorpayError as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": f"Payment error: {str(e)}"}, status=400)
            messages.error(request, f"Payment error: {str(e)}")
            return redirect('subscription')
        except Exception as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)
            messages.error(request, f"An unexpected error occurred: {str(e)}")
            return redirect('subscription')

    if is_ajax:
        return JsonResponse({"status": "error", "message": "POST required"}, status=405)
    return redirect('subscription')
