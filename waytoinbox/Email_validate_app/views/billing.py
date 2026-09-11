from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, FileResponse
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.core.cache import cache
from django.views.decorators.http import require_POST
from django.db import transaction, IntegrityError
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
    UserTable, ListFiles, SubsPayment, Payment, ServiceOrder, ServiceCreditLot,
    CurrentCredits, TotalCredits, UsedCredits, AllEmails,
)
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.mailer import send_payment_success_email

logger = logging.getLogger(__name__)

_WIN_TABLE_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')

# ── Phase 2 (PAYG purchase hardening) ───────────────────────────────────────
# Legacy Email Validation Pay-As-You-Go purchase quantity/amount is now
# frozen server-side into a ServiceOrder row (flow='payg_ev') at order
# creation, exactly mirroring the pattern views/credits.py already uses for
# the new service-credit checkout. Verification (payment(), below) reads
# quantity/amount back from THIS row -- never from POST -- so a browser
# posting a different `credits`/`price`/`amount` can no longer change what
# gets granted. The grant destination is UNCHANGED: still legacy
# CurrentCredits.vc via insert_vc_credits(); routing PAYG through
# ServiceCreditLot is a later phase's work, not this one's.

# $1.00, matching order_payment's existing minimum -- unchanged from today.
MIN_PAYG_ORDER_CENTS = 100


class PaygOrderError(Exception):
    """Raised by _create_payg_ev_order() with an already user-facing
    message -- callers relay str(e) exactly the way they already relay
    individual Razorpay error messages today."""


def _create_payg_ev_order(user_id, quantity, discount_percentage=0, timezone_str='Asia/Kolkata'):
    """Server-authoritative EV PAYG order creation, shared by
    order_payment(), download_results() and verify_emails()'s need-credits
    branch.

    `quantity` is the caller's already-validated positive int credit count.
    Price comes from calculate_price() (the existing, real EV pricing
    function) -- never from the browser. `discount_percentage` lets
    order_payment() keep applying its existing legacy-plan discount; the
    other two callers pass 0 (their existing, unchanged behavior).

    Returns (razorpay_order_dict, service_order). Raises PaygOrderError
    with a ready-to-display message on any validation/gateway failure --
    nothing is created in that case.
    """
    quantity = int(quantity)
    if quantity <= 0:
        raise PaygOrderError("Credit quantity must be positive.")

    ok, result = calculate_price(quantity)
    if not ok:
        raise PaygOrderError(str(result))
    base_price, _rate = result

    discounted_price = (
        round(base_price - (base_price * discount_percentage / 100), 2)
        if discount_percentage else base_price
    )
    subtotal_cents = int(round(base_price * 100))
    amount_cents   = int(round(discounted_price * 100))

    if amount_cents < MIN_PAYG_ORDER_CENTS:
        raise PaygOrderError(f"Order amount must be at least ${MIN_PAYG_ORDER_CENTS / 100:,.2f}.")

    receipt_id = generate_receipt_id(timezone_str)
    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    try:
        rz_order = client.order.create(data={
            "amount":   amount_cents,
            # Always USD -- the "usd-inr" form field is a hidden, hardcoded
            # "USD" input in both templates today, never a real selector,
            # so there is nothing legitimate to read from POST here.
            "currency": "USD",
            "receipt":  receipt_id,
        })
    except BadRequestError as e:
        logger.error("Razorpay bad request error (PAYG EV): %s", e)
        raise PaygOrderError("Invalid request to payment gateway.") from e
    except ServerError as e:
        logger.error("Razorpay server error (PAYG EV): %s", e)
        raise PaygOrderError("Payment gateway server error.") from e
    except razorpay_errors.RazorpayError as e:
        logger.error("Razorpay error (PAYG EV): %s", e)
        raise PaygOrderError("Payment could not be initiated. Please try again.") from e

    service_order = ServiceOrder.objects.create(
        user_id=user_id,
        order_id=rz_order['id'],
        flow=ServiceOrder.FLOW_PAYG_EV,
        cart_json={'email_validation': quantity},
        subtotal_cents=subtotal_cents,
        discount_cents=max(0, subtotal_cents - amount_cents),
        amount_cents=amount_cents,
        currency='USD',
    )
    return rz_order, service_order


def _drop_win_table(table_name: str) -> None:
    """Drop orphaned dynamic WIN_* table after job soft-delete (DB-11). Pattern-validated before execution."""
    if not table_name or not _WIN_TABLE_RE.fullmatch(table_name):
        return
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS `%s`' % table_name)  # nosec: pattern-validated above

# Names used by this module, plus the ones other view modules re-import from
# here (views/subscription.py takes get_ac_current_credit and the three
# insert_*_credits; several take get_current_credit / calculate_price /
# generate_receipt_id). Phase 6 commit 8 dropped seven imports that were dead
# both locally and as re-exports: get_vc_current_credit, get_ip_current_credit,
# update_or_insert_current_credit, insert_ip_credits, and the three
# deduct_vc/ac/cc_credits. Commit 11 then deleted
# update_or_insert_current_credit and insert_ip_credits outright, having
# confirmed zero references repo-wide; the deduct_* trio is kept.
# Phase 2 (PAYG purchase hardening) removed insert_credits: payment()'s only
# caller now calls insert_vc_credits() directly so it can pass ref_id=order_id
# (insert_credits's thin ref_type='payg' wrapper never took a ref_id at all).
from Email_validate_app.services.credit_manager import (
    generate_receipt_id,
    get_current_credit, get_ac_current_credit,
    insert_vc_credits,
    insert_ac_credits, insert_cc_credits,
    calculate_price, manage_credits,
)


# Binds a legacy (non-service-credit) Razorpay order to the account that
# created it, so that switching the active Main/Sub Account context between
# order creation and verification can never move a payment/credits to a
# different account than the one that started the purchase. Mirrors the
# invariant views/credits.py already gets for free from its ServiceOrder
# row (order_id + user_id, checked together at verify time) -- these two
# legacy flows never persisted a creator identity anywhere, so a lightweight
# cache entry is the smallest fix that doesn't touch Payment/SubsPayment's
# existing "only real completed payments" semantics. TTL is generous (1
# hour) so a normal, slow checkout never gets rejected; a peek (not pop) so
# a legitimate double-click/retry by the same account still finds it.
#
# Phase 2 (PAYG purchase hardening): order_payment()/download_results()/
# verify_emails() now record ownership via ServiceOrder.user instead (a
# permanent DB row, not a 1-hour cache entry), so _remember_legacy_order_owner
# below has no remaining caller. Kept, not deleted, alongside its still-used
# read counterpart (_get_legacy_order_owner, still read by subs_payment())
# and create_subscription() (Phase 2 blocked its order creation -- see that
# view's docstring). Both belong to the SubsPayment plan-purchase flow's
# existing deprecation posture: fail closed, don't delete.
_LEGACY_ORDER_OWNER_TTL = 3600


def _remember_legacy_order_owner(order_id, user_id):
    cache.set(f'legacy_order_owner:{order_id}', user_id, _LEGACY_ORDER_OWNER_TTL)


def _get_legacy_order_owner(order_id):
    return cache.get(f'legacy_order_owner:{order_id}') if order_id else None


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

    # ── Service-credit purchase card (Subscription tab) ─────────────────────
    # Deliberately duplicated from views/subscription.py::subscription rather
    # than imported/refactored, so that view and i_subscription.html stay
    # completely untouched — this page now embeds its own copy of the same
    # cards. Keep the two in sync by hand if the card's data shape ever
    # changes.
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

    return render(request, "i_pricing.html", {
        "credits": current_credits,
        "active_plan": active_plan,
        "services": services,
        "legacy_shared": legacy_shared,
        "pricing_config": public_config(),
    })


def order_payment(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method != "POST":
        if is_ajax:
            return JsonResponse({"status": "error", "message": "POST required"}, status=405)
        messages.error(request, "Invalid request method.")
        return redirect('pricing')

    try:
        # SEC-03 (Phase 2): `credits` is still read from POST here -- the
        # user's REQUESTED quantity -- but it is now only an input to
        # server-side pricing/validation (_create_payg_ev_order), never a
        # value that is itself trusted for the final grant. `price`,
        # `pricePerEmail` and the hidden `usd-inr` field are no longer read
        # at all: the server computes price via calculate_price() and
        # always charges in USD (see _create_payg_ev_order).
        credits = request.POST.get("plan")
        timezone_str = request.GET.get('timezone', 'Asia/Kolkata')

        logger.debug("credits requested=%s", credits)

        user_id = get_user_id(request)
        current_credits = get_current_credit(user_id)

        if not credits:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Credits must be provided."}, status=400)
            messages.warning(request, "Credits must be provided.")
            return redirect('subscription')

        try:
            credits = int(credits)
        except ValueError:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid input for credits."}, status=400)
            messages.error(request, "Invalid input for credits.")
            return redirect('subscription')

        if credits <= 0:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Credits must be a positive number."}, status=400)
            messages.error(request, "Credits must be a positive number.")
            return redirect('subscription')

        user_data = fetch_user_data(user_id)
        if not user_data:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found. Please log in."}, status=404)
            messages.error(request, "User not found. Please log in.")
            return redirect('subscription')

        if not user_data.is_verified:
            if is_ajax:
                return JsonResponse({"status": "error",
                                     "message": "Please verify your email before purchasing.",
                                     "reason": "not_verified"}, status=403)
            messages.error(request, "Please verify your email before purchasing.")
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

        # Apply discount by plan -- unchanged business logic, still entirely
        # server-derived (subs_plan comes from the DB, never from POST).
        discount_percentage = 0
        if subs_plan:
            plan = subs_plan.strip().lower()
            if plan == "classic":
                discount_percentage = 2
            elif plan == "standard":
                discount_percentage = 4
            elif plan == "advanced":
                discount_percentage = 8

        try:
            rz_order, service_order = _create_payg_ev_order(
                user_id, credits, discount_percentage=discount_percentage,
                timezone_str=timezone_str,
            )
        except PaygOrderError as e:
            if is_ajax:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)
            messages.error(request, str(e))
            return redirect('subscription')

        rz_order['display_amount'] = rz_order['amount'] / 100
        discounted_price = service_order.amount_cents / 100
        # Cosmetic per-email rate for receipt/UI display only -- server-
        # derived from the same frozen amount, never used in any grant or
        # charge calculation.
        plan_display = f"{discounted_price / credits:.6f}" if credits else "0"

        if is_ajax:
            return JsonResponse({
                "status":              "ok",
                "key_id":              settings.RAZORPAY_KEY_ID,
                "order_id":            rz_order['id'],
                "amount":              rz_order['amount'],
                "currency":            rz_order.get('currency', 'USD'),
                "user_name":           user_data.user_name,
                "user_email":          user_data.user_email,
                "user_id":             user_data.id,
                "credit":              credits,
                "plan":                plan_display,
                "discount_percentage": discount_percentage,
                "flow":                "payg",
            })

        return render(request, "i_payment_2.html", {
            "credits":             current_credits,
            "payment":             rz_order,
            "user_data":           user_data,
            "credit":              credits,
            "currency":            "USD",
            "plan":                plan_display,
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
    """Verify a legacy EV PAYG payment and grant exactly the quantity
    frozen server-side at order creation.

    SEC-03 (Phase 2): `credits`, `amount`, `currency` and `plan` are no
    longer read from POST at all -- they are read back from the
    ServiceOrder(flow='payg_ev') row that _create_payg_ev_order() created
    before Razorpay Checkout ever opened. A browser posting a different
    credits/amount/currency/plan cannot change what gets granted, because
    those values are never consulted here. Only order_id/payment_id/
    razorpay_signature/description/user_name are still taken from the
    request -- description and user_name are stored verbatim only as
    display-only receipt fields, never used in any grant/charge decision.
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method != 'POST':
        if is_ajax:
            return JsonResponse({"status": "error", "message": "POST required"}, status=405)
        return redirect('pricing')

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
    description        = request.POST.get('description')
    payer_name         = request.POST.get('user_name')

    if not order_id:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Invalid payment data."}, status=400)
        messages.error(request, "Invalid payment data.")
        return redirect('pricing')

    # SEC-03 (Phase 2): the ServiceOrder this order_id belongs to is the
    # sole source of truth for quantity/amount/currency, and its `user` FK
    # is the sole source of truth for ownership -- replacing the old
    # cache-based _get_legacy_order_owner() lookup, which order_payment()/
    # download_results()/verify_emails() no longer write to. Scoping the
    # lookup to user_id means a different account's order_id simply
    # doesn't exist from this user's point of view -- same rejection
    # message as before, now backed by a permanent DB row instead of a
    # 1-hour cache entry (this also fixes a pre-existing bug: verify_emails
    # never called _remember_legacy_order_owner, so its own top-up flow
    # always failed this check).
    try:
        service_order = ServiceOrder.objects.get(
            order_id=order_id, user_id=user_id, flow=ServiceOrder.FLOW_PAYG_EV,
        )
    except ServiceOrder.DoesNotExist:
        logger.warning(
            "PAYG verify for unknown/foreign order: order=%s user=%s", order_id, user_id)
        if is_ajax:
            return JsonResponse({"status": "error",
                                 "message": "This order was started under a different account. Please start a new purchase."},
                                status=403)
        messages.error(request, "This order was started under a different account. Please start a new purchase.")
        return redirect('pricing')

    # SEC-02: verify Razorpay payment signature before crediting (unchanged)
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

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

    try:
        payment_details = client.payment.fetch(payment_id)

        if payment_details:
            payer_email = payment_details.get("email")
            customer_contact = payment_details.get("contact")
            paid_amount_cents = int(payment_details.get('amount', 0))
        else:
            payer_email = request.POST.get('user_email')
            customer_contact = request.POST.get('user_contact')
            paid_amount_cents = None

        # SEC-03 (Phase 2/Step 7): the amount actually charged by Razorpay
        # must equal what this order was created for -- belt-and-suspenders
        # on top of the signature check above, which already cryptographically
        # ties payment_id to order_id. Nothing is granted if they disagree.
        if paid_amount_cents is not None and paid_amount_cents != service_order.amount_cents:
            logger.error(
                "PAYG amount mismatch: order=%s expected=%s paid=%s user=%s",
                order_id, service_order.amount_cents, paid_amount_cents, user_id)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Payment amount mismatch. Please contact support."}, status=400)
            messages.error(request, "Payment amount mismatch. Please contact support.")
            return redirect('pricing')

        # Authoritative quantity/amount/currency -- from the frozen order,
        # never from POST.
        quantity   = int((service_order.cart_json or {}).get('email_validation', 0))
        amount     = f"{service_order.amount_cents / 100:.2f}"
        currency   = service_order.currency
        unit_price = f"{(service_order.amount_cents / 100 / quantity):.6f}" if quantity else "0"

        if quantity <= 0:
            logger.error("PAYG order %s has no email_validation quantity; refusing to grant", order_id)
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Invalid order. Please contact support."}, status=400)
            messages.error(request, "Invalid order. Please contact support.")
            return redirect('pricing')

        current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)
        payment_time = current_datetime

        # SEC-03 (Phase 2/Step 6): lock the frozen order BEFORE deciding
        # whether it has already been finalized -- mirrors views/credits.py::
        # subscription_verify()'s ServiceOrder->Payment lock order exactly,
        # so a replayed/concurrent verification of the SAME order_id can
        # never grant twice. The pre-existing Payment.order_id unique
        # constraint + IntegrityError catch below is kept as a second,
        # independent safeguard -- not a replacement for this lock.
        with transaction.atomic():
            locked_order = ServiceOrder.objects.select_for_update().get(pk=service_order.pk)

            if locked_order.status == ServiceOrder.STATUS_PAID:
                logger.info("PAYG order %s already fulfilled; ignoring replay", order_id)
                if is_ajax:
                    return JsonResponse({"status": "ok", "message": "Payment already processed."})
                messages.info(request, "This payment was already processed.")
                return redirect('pricing')

            # DB-08: idempotency guard — prevent double-crediting on retry or double-click (kept, unchanged)
            if Payment.objects.filter(order_id=order_id).exists():
                logger.warning("Duplicate payment attempt blocked: order=%s user=%s", order_id, user_id)
                if is_ajax:
                    return JsonResponse({"status": "ok", "message": "Payment already processed."})
                messages.info(request, "This payment was already processed.")
                return redirect('pricing')

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
                    unit_price=unit_price,
                    amount=amount,
                    currency=currency,
                    credits=str(quantity),
                    payment_time=payment_time,
                    description=description,
                )
                payment_obj.save()
            except IntegrityError:
                # DB-08b: concurrent retry raced past the exists() check; the
                # unique constraint on order_id caught it — treat as already-processed.
                logger.warning("Concurrent payment race caught by unique constraint: order=%s", order_id)
                if is_ajax:
                    return JsonResponse({"status": "ok", "message": "Payment already processed."})
                messages.info(request, "This payment was already processed.")
                return redirect('pricing')

            # Grant destination is UNCHANGED — still legacy CurrentCredits.vc.
            # Routing PAYG through ServiceCreditLot is a later phase's work.
            insert_vc_credits(request, user_id, quantity, ref_type='payg', ref_id=order_id)

            locked_order.status  = ServiceOrder.STATUS_PAID
            locked_order.paid_at = payment_time
            locked_order.save(update_fields=['status', 'paid_at'])

        if getattr(user, 'notify_payment', True):
            send_payment_success_email(
                user_name=payer_name,
                user_email=user.user_email,
                amount=amount,
                currency=currency,
                order_id=order_id,
                payment_time=payment_time,
                extra={'type': 'payg', 'credits': quantity},
            )
        from Email_validate_app.utils import create_notification
        create_notification(user_id, 'payment',
            f"Payment of {currency} {amount} received — {quantity} email credits added",
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

            try:
                user_data = UserTable.objects.get(id=user_id)
            except UserTable.DoesNotExist:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)

            if not user_data.is_verified:
                return JsonResponse(
                    {"status": "error", "message": "Please verify your email before purchasing.",
                     "reason": "not_verified"}, status=403)

            # SEC-03 (Phase 2): need_c is already fully server-derived above
            # (manage_credits()'s shortfall + the existing +150 top-up rule);
            # _create_payg_ev_order() now freezes that exact quantity and its
            # price into a ServiceOrder row instead of a bare Razorpay order
            # + 1-hour cache entry, so payment()'s verification reads the
            # quantity back from there rather than trusting a repeated POST.
            try:
                rz_order, service_order = _create_payg_ev_order(user_id, need_c)
            except PaygOrderError as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)

            plan_display = f"{(service_order.amount_cents / 100) / need_c:.6f}" if need_c else "0"

            return JsonResponse({
                "status":    "need_credits",
                "key_id":    settings.RAZORPAY_KEY_ID,
                "order_id":  rz_order['id'],
                "amount":    rz_order['amount'],
                "currency":  rz_order.get('currency', 'USD'),
                "user_name": user_data.user_name,
                "user_email": user_data.user_email,
                "user_id":   user_data.id,
                "credit":    need_c,
                "plan":      plan_display,
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
    # Anonymous submissions are allowed (e.g. a visitor blocked at signup,
    # who by definition has no session) alongside the pre-existing
    # logged-in path — the form's own name/email/message fields are
    # already independent of any logged-in user's identity.
    from django.core.mail import send_mail

    user_id = get_user_id(request)
    user = fetch_user_data(user_id) if user_id else None

    name = request.POST.get('name')
    email = request.POST.get('email')
    message = request.POST.get('message')

    subject = f"Customer Support Request from {name}"
    full_message = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"User Id: {user_id or 'N/A (not logged in)'}\n"
        f"User Name: {user.user_name if user else 'N/A (not logged in)'}\n"
        f"User Email: {user.user_email if user else 'N/A (not logged in)'}\n"
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
