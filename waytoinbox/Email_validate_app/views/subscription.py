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

# Phase 2 (PAYG purchase hardening) removed create_subscription()'s order-
# creation body (see that view's docstring), which was the only caller of
# generate_receipt_id/_remember_legacy_order_owner in this module.
from .billing import (
    get_ac_current_credit,
    _razorpay_payer_method,
    insert_vc_credits,
    insert_ac_credits,
    insert_cc_credits,
    _get_legacy_order_owner,
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

    # ── Service-credit purchase card ──────────────────────────────────────
    # Balances are read, never created: no get_or_create here or in the
    # template. A user with no ServiceCredit row simply renders 0.
    #
    # Only the NEW per-service balance is exposed per row. The 'effective'
    # figure from get_all_service_balances() adds the legacy pool in, and for
    # the four analysis services that pool is ONE shared AC balance — showing
    # it on all four rows would tell a user with 100 AC that they have 400.
    # It is surfaced once, separately, as "Shared Analysis Credits".
    from Email_validate_app.services.credit_manager import get_all_service_balances
    from Email_validate_app.services.pricing import (
        SERVICE_LABELS, SERVICE_UNITS, SERVICE_MIN_QTY, public_config,
    )
    from Email_validate_app.services.trial_manager import TRIAL_LIMITS
    from Email_validate_app.models import SERVICE_KEYS

    try:
        balances = get_all_service_balances(user_id) if user_id else None
    except Exception as e:
        logger.error("Error fetching service balances: %s", e)
        balances = None

    new_balances  = (balances or {}).get('services', {})
    legacy_shared = (balances or {}).get('legacy_shared', {})

    services = [
        {
            'key':         key,
            'label':       SERVICE_LABELS[key],
            'unit':        SERVICE_UNITS.get(key, 'credits'),
            'balance':     new_balances.get(key, {}).get('new', 0),
            'min_qty':     SERVICE_MIN_QTY[key],
            # For the free-trial popup's limits list -- not the purchase
            # card, which never shows a trial figure of its own.
            'trial_limit': TRIAL_LIMITS[key],
        }
        for key in SERVICE_KEYS
    ]

    return render(request, "i_subscription.html", {
        "credits": ip_current_credits,
        "active_plan": active_plan,
        "subs_plan": active_plan["subs_plan"] if active_plan else None,
        "services": services,
        "legacy_shared": legacy_shared,
        # Display-only mirror of the ladders so the total can update instantly
        # while the debounced quote is in flight. Carries no per-credit rate —
        # public_config() deliberately exposes whole-package prices only — and
        # the server re-quotes at order time regardless. Rendered through the
        # json_script filter, so it is escaped rather than inlined raw.
        "pricing_config": public_config(),
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
    """Legacy Classic/Standard/Advanced plan purchase — order creation.

    SEC-03 (Phase 2, Step 10): confirmed unreachable from any current
    template (no UI anywhere posts to this view; the pricing/subscription
    pages were rebuilt around the new service-credit checkout in
    views/credits.py long ago) but the route itself was still directly
    POST-callable and trusted `price` from the request with no check
    against any real price table, and an unrecognized `plan` value fell
    through to the most generous credit tier at whatever price was posted.

    Per Phase 2's instruction to prefer a safe server-side rejection over
    silently deleting still-present code: this view now refuses to create
    any new order. subs_payment() (the matching verify step) is untouched
    and needs no change — with no new order ever created here again, its
    own ownership check can never find a match, so it fails closed on its
    own. Nothing here deletes the model, the URL, the templates, or
    subs_payment()/SubsPayment (still needed for expiry/receipts/admin).
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        message = ("Purchasing a plan this way is no longer available. "
                   "Please use the Subscription page to buy credits.")
        logger.warning(
            "Blocked deprecated create_subscription POST (plan=%r, price=%r) from user=%s",
            request.POST.get('plan'), request.POST.get('price'), get_user_id(request))
        if is_ajax:
            return JsonResponse({"status": "error", "message": message}, status=410)
        messages.error(request, message)
        return redirect('subscription')

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

        # SEC-XX: the account active now must be the same one that created
        # this order -- see billing.py::payment()'s identical check for the
        # full rationale. create_subscription() records the creator here.
        order_owner_id = _get_legacy_order_owner(order_id)
        if order_owner_id != user_id:
            logger.warning(
                "Subs payment order/account mismatch: order=%s created_by=%s current_active=%s",
                order_id, order_owner_id, user_id)
            if is_ajax:
                return JsonResponse({"status": "error",
                                     "message": "This order was started under a different account. Please start a new purchase."},
                                    status=403)
            messages.error(request, "This order was started under a different account. Please start a new purchase.")
            return redirect('subscription')

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
