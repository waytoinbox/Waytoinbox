"""Service-credit checkout: quote, order, verify.

Three endpoints, one rule: **the browser supplies quantities and nothing
else.** Price, discount, currency, credit amounts and the final charge are all
computed server-side, frozen into a ServiceOrder row at order time, and read
back from that row at verification time. A tampered verify POST therefore
cannot change what is credited.

This is a new entry point alongside the legacy PAYG flow in views/billing.py
(order_payment / payment) and the plan flow in views/subscription.py
(create_subscription / subs_payment). None of those are modified — they keep
working for existing plans and in-flight payments.

Deliberate differences from the legacy flow:

  * Money is integer cents end to end. billing.order_payment does
    `int(discounted_price * 100)` on a float, which is the rounding-bug class
    this avoids.
  * `credits` is never read from request.POST at verify time
    (billing.payment does exactly that).
  * Currency is a server constant, not a hidden form input the page supplies.
  * Fulfilment is exactly-once via select_for_update() on the order row plus a
    status transition, rather than an .exists() pre-check.
"""
import json
import logging
from datetime import datetime

import pytz
import razorpay
from razorpay.errors import BadRequestError, ServerError, SignatureVerificationError
import razorpay.errors as razorpay_errors

from django.conf import settings
from django.db import transaction, IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from Email_validate_app.models import UserTable, Payment, ServiceOrder
from Email_validate_app.services import pricing
from Email_validate_app.services import coupon_service
from Email_validate_app.services.credit_manager import (
    add_service_credits, generate_receipt_id, get_all_service_balances,
)
from Email_validate_app.services.mailer import send_payment_success_email
from Email_validate_app.services.trial_manager import (
    TRIAL_DURATION_DAYS, TRIAL_LIMITS, SERVICE_LABELS as TRIAL_SERVICE_LABELS,
    activate_trial, has_ever_paid, is_trial_eligible,
)
from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)

# Server-owned. The legacy flow takes this from a hidden <input> the page
# supplies, which means the browser picks the currency it is charged in.
CURRENCY = 'USD'

# Razorpay rejects orders below a small floor, and a $0 order is meaningless.
MIN_ORDER_CENTS = 100  # $1.00


def _razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _read_cart(request):
    """Pull {service: qty} out of the request.

    Accepts a JSON body {"cart": {...}} or a form post with one field per
    service. Returns the raw dict; pricing.quote_cart does the validating, so
    unknown keys and junk quantities are rejected in exactly one place.
    """
    if request.content_type and 'application/json' in request.content_type:
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            raise pricing.PricingError("Malformed request.")
        if not isinstance(payload, dict):
            raise pricing.PricingError("Malformed request.")
        cart = payload.get('cart', {})
        promo = (payload.get('promo_code') or '').strip()
    else:
        cart = {k: v for k, v in request.POST.items() if k in pricing.SERVICE_KEYS}
        promo = (request.POST.get('promo_code') or '').strip()

    if not isinstance(cart, dict):
        raise pricing.PricingError("Malformed cart.")
    return cart, promo


def _quote_discount(quote, promo_code, user_id):
    """Resolve a promo code against a quote.

    Returns (discount_cents, message, coupon). Read-only: nothing is reserved,
    no counter moves, no redemption row is written. An invalid code yields a
    zero discount and a reason — it never fails the request, because a shopper
    mistyping a code should still see their cart priced.

    The same function backs both the live quote and order creation, so the
    discount shown can never differ from the discount charged.
    """
    if not promo_code:
        return 0, '', None

    user = None
    if user_id:
        user = UserTable.objects.filter(id=user_id).first()

    ok, discount_cents, reason = coupon_service.validate_coupon(
        promo_code, user, quote.subtotal_cents, quote=quote)

    if not ok:
        return 0, reason, None

    from Email_validate_app.models import Coupon
    coupon = Coupon.objects.filter(
        code=coupon_service.normalize_code(promo_code)).first()
    return discount_cents, reason, coupon


# -- Endpoints ---------------------------------------------------------------

@require_POST
def subscription_quote(request):
    """Price a cart for live display. Reads nothing, writes nothing.

    Returns the same numbers /subscription/order/ will use, so the total the
    user sees is the total they are charged.

    Deliberately does NOT require login: seeing a price must never depend on
    being signed in — only actually starting checkout (subscription_order)
    does. user_id is still resolved (possibly None) so a signed-in user's
    own promo-code usage limits are honoured; _quote_discount()/
    validate_coupon() already treat a None user as "skip the per-user check"
    rather than erroring.
    """
    user_id = get_user_id(request)

    try:
        cart, promo_code = _read_cart(request)
        quote = pricing.quote_cart(cart, currency=CURRENCY)
    except pricing.PricingError as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)

    discount_cents, promo_message, coupon = _quote_discount(quote, promo_code, user_id)
    total_cents = max(0, quote.subtotal_cents - discount_cents)

    data = quote.as_dict()
    data.update({
        "status":          "ok",
        "subtotal_cents":  quote.subtotal_cents,
        "subtotal":        f"${quote.subtotal_cents / 100:,.2f}",
        "discount_cents":  discount_cents,
        "discount":        f"${discount_cents / 100:,.2f}",
        "total_cents":     total_cents,
        "total":           f"${total_cents / 100:,.2f}",
        # Echoed back only when the code actually validated, so the UI can
        # label the discount without re-deciding whether it applied.
        "promo_code":      coupon.code if coupon else '',
        "promo_applied":   coupon is not None,
        "promo_message":   promo_message,
        "min_order_cents": MIN_ORDER_CENTS,
    })
    return JsonResponse(data)


@require_POST
def subscription_order(request):
    """Re-quote server-side, create the Razorpay order, freeze it in a row.

    Nothing is credited here. The response carries only what Checkout.js needs
    to open; the amount it shows comes from the Razorpay order, which was
    created from the server's own arithmetic.

    Any nonempty subset of the 7 services may be purchased — a service left
    at 0 is simply not selected, not an error (see pricing.quote_cart()).
    Only a fully empty cart (every service at 0) is rejected below. Each
    selected (nonzero) service still must clear its own existing minimum,
    enforced the same way it always was, inside quote_cart() itself.
    """
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Not authenticated."}, status=401)

    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return JsonResponse({"status": "error", "message": "User not found."}, status=404)

    if not user.is_verified:
        return JsonResponse(
            {"status": "error", "message": "Please verify your email before purchasing.",
             "reason": "not_verified"}, status=403)

    try:
        cart, promo_code = _read_cart(request)
        quote = pricing.quote_cart(cart, currency=CURRENCY)
    except pricing.PricingError as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)

    if not quote.lines:
        return JsonResponse(
            {"status": "error", "message": "Select at least one service to continue."},
            status=400)

    discount_cents, promo_message, coupon = _quote_discount(quote, promo_code, user_id)
    amount_cents = max(0, quote.subtotal_cents - discount_cents)

    # A code that was typed but did not survive validation is refused here
    # rather than silently ignored, so nobody pays full price believing a
    # discount was applied.
    if promo_code and coupon is None:
        return JsonResponse(
            {"status": "error",
             "message": promo_message or "That promo code is not valid."},
            status=400)

    if amount_cents < MIN_ORDER_CENTS:
        return JsonResponse(
            {"status": "error",
             "message": f"Order total must be at least ${MIN_ORDER_CENTS / 100:,.2f}."},
            status=400)

    # The cart is re-derived from the validated quote, not echoed from the
    # request, so only priced services at normalised integer quantities are
    # ever stored.
    frozen_cart = {ln.service: ln.quantity for ln in quote.lines}

    try:
        rz_order = _razorpay_client().order.create(data={
            "amount":   amount_cents,          # already an integer, never a float
            "currency": CURRENCY,
            "receipt":  generate_receipt_id(request.GET.get('timezone', 'Asia/Kolkata')),
            "notes":    {"flow": "service_credits", "user_id": str(user_id)},
        })
    except BadRequestError as e:
        logger.error("Razorpay bad request creating service order for user %s: %s", user_id, e)
        return JsonResponse(
            {"status": "error", "message": "Invalid request to payment gateway."}, status=400)
    except ServerError as e:
        logger.error("Razorpay server error creating service order for user %s: %s", user_id, e)
        return JsonResponse(
            {"status": "error", "message": "Payment gateway server error."}, status=502)
    except razorpay_errors.RazorpayError as e:
        logger.error("Razorpay error creating service order for user %s: %s", user_id, e)
        return JsonResponse(
            {"status": "error", "message": "Payment could not be initiated. Please try again."},
            status=500)

    try:
        ServiceOrder.objects.create(
            user=user, order_id=rz_order['id'], cart_json=frozen_cart,
            subtotal_cents=quote.subtotal_cents, discount_cents=discount_cents,
            amount_cents=amount_cents, currency=CURRENCY, promo_code=promo_code[:50],
            coupon=coupon,
        )
    except IntegrityError:
        # Razorpay ids are unique; this would mean a genuine collision or a
        # double-submit that already produced the row. Either way it is safe
        # to continue to checkout with the existing order.
        logger.warning("ServiceOrder already exists for order %s", rz_order['id'])

    logger.info("Service order %s created for user %s: %s cents, cart=%s",
                rz_order['id'], user_id, amount_cents, frozen_cart)

    return JsonResponse({
        "status":         "ok",
        "key_id":         settings.RAZORPAY_KEY_ID,
        "order_id":       rz_order['id'],
        "amount_cents":   amount_cents,
        "amount":         f"${amount_cents / 100:,.2f}",
        "currency":       CURRENCY,
        "user_name":      user.user_name,
        "user_email":     user.user_email,
        "flow":           "service_credits",
        # For the "Confirm your purchase" / "Order Summary" popup
        # (openServiceConfirm in the page's own script): one line per
        # selected service for the "Confirm your purchase" column, a
        # general-purpose human-readable summary string (used elsewhere,
        # e.g. Payment.credits at verify time), and a ready-made discount
        # label — the dollar-off amount, since a coupon's discount is not
        # generally a clean percentage the way Pay-As-You-Go's plan
        # discounts are.
        "lines": [
            {"service": ln.service, "label": ln.label, "quantity": ln.quantity}
            for ln in quote.lines
        ],
        "credit":         _credits_summary(frozen_cart),
        "discount_label": f"−${discount_cents / 100:,.2f} off" if discount_cents else '',
    })


@require_POST
def subscription_verify(request):
    """Verify the signature, then credit strictly what the order row says.

    Everything that mutates state happens in one transaction, under a row lock
    on the order. The only inputs taken from the browser are the three
    Razorpay identifiers, and those are worthless without a valid signature.
    """
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Not authenticated."}, status=401)

    if request.content_type and 'application/json' in request.content_type:
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({"status": "error", "message": "Malformed request."}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"status": "error", "message": "Malformed request."}, status=400)
    else:
        payload = request.POST

    order_id   = payload.get('razorpay_order_id')   or payload.get('order_id')
    payment_id = payload.get('razorpay_payment_id') or payload.get('payment_id')
    signature  = payload.get('razorpay_signature')  or ''

    if not (order_id and payment_id and signature):
        logger.warning("Service verify missing fields: order=%s payment=%s user=%s",
                       order_id, payment_id, user_id)
        return JsonResponse({"status": "error", "message": "Invalid payment data."}, status=400)

    # Scoped to the session user, so one account cannot claim another's order.
    try:
        order = ServiceOrder.objects.get(order_id=order_id, user_id=user_id)
    except ServiceOrder.DoesNotExist:
        logger.warning("Service verify for unknown order %s (user %s)", order_id, user_id)
        return JsonResponse({"status": "error", "message": "Unknown order."}, status=404)

    try:
        _razorpay_client().utility.verify_payment_signature({
            'razorpay_order_id':   order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature':  signature,
        })
    except SignatureVerificationError:
        logger.warning("Service payment signature failed: order=%s payment=%s user=%s",
                       order_id, payment_id, user_id)
        ServiceOrder.objects.filter(pk=order.pk, status=ServiceOrder.STATUS_CREATED) \
                            .update(status=ServiceOrder.STATUS_FAILED)
        return JsonResponse(
            {"status": "error", "message": "Payment verification failed."}, status=400)

    # Payer details are informational only — they never influence the credit
    # amount, so a gateway hiccup here must not block fulfilment.
    payer_email = customer_contact = None
    payer_method = "Razorpay"
    try:
        details = _razorpay_client().payment.fetch(payment_id)
        if details:
            payer_email      = details.get("email")
            customer_contact = details.get("contact")
            from .billing import _razorpay_payer_method
            payer_method = _razorpay_payer_method(details)
    except razorpay_errors.RazorpayError as e:
        logger.warning("Could not fetch payment %s details: %s", payment_id, e)

    amount_str = f"{order.amount_cents / 100:.2f}"

    try:
        with transaction.atomic():
            locked = ServiceOrder.objects.select_for_update().get(pk=order.pk)

            if locked.status == ServiceOrder.STATUS_PAID:
                logger.info("Service order %s already fulfilled; ignoring replay", order_id)
                return JsonResponse({"status": "ok", "message": "Payment already processed.",
                                     "already_processed": True})

            payment_obj = Payment.objects.create(
                user_id=user_id,
                order_id=order_id,
                payment_id=payment_id,
                payer_id=payment_id,
                payer_name=_payer_name(payload, user_id),
                payer_email=payer_email,
                payer_address=customer_contact,
                payer_method=payer_method,
                amount=amount_str,
                currency=locked.currency,
                credits=_credits_summary(locked.cart_json),
                description="Service credits",
                cart_json=locked.cart_json,
                promo_code=locked.promo_code,
                discount_amount=(locked.discount_cents / 100) if locked.discount_cents else None,
            )

            # Redemption happens here and nowhere else — never at quote or
            # order time. It re-checks max_uses and per_user_limit while
            # holding a row lock on the coupon, so two concurrent checkouts
            # cannot both consume the last remaining use.
            #
            # This runs BEFORE the credits are added so that every writer takes
            # locks in the same order:
            #   ServiceOrder -> Coupon -> ServiceCredit -> CurrentCredits
            # Moving it below add_service_credits() would invert the last two
            # and open a deadlock between concurrent checkouts. Keep it here.
            coupon_service.redeem_coupon(locked, payment_obj, user_id=user_id)

            # THE authoritative step: quantities come from the order row, which
            # was written from a server-side quote — never from this request.
            for service, qty in locked.cart_json.items():
                label = pricing.SERVICE_LABELS.get(service, service)
                add_service_credits(
                    user_id, service, int(qty),
                    ref_type='service_purchase', ref_id=order_id,
                    description=f"Purchased {int(qty):,} {label} credits")

            locked.status  = ServiceOrder.STATUS_PAID
            locked.paid_at = timezone.now()
            locked.payment = payment_obj
            locked.save(update_fields=['status', 'paid_at', 'payment'])
    except IntegrityError:
        # A concurrent request won the race and created the Payment row first;
        # order_id is unique, so this one is a duplicate. Nothing was credited
        # by this request — the transaction rolled back.
        logger.warning("Concurrent service verify caught by unique constraint: order=%s", order_id)
        return JsonResponse({"status": "ok", "message": "Payment already processed.",
                             "already_processed": True})

    logger.info("Service order %s fulfilled for user %s: %s", order_id, user_id, order.cart_json)

    _notify(user_id, order, amount_str)

    return JsonResponse({
        "status":   "ok",
        "order_id": order_id,
        "amount":   f"${order.amount_cents / 100:,.2f}",
        "balances": get_all_service_balances(user_id),
    })


@require_POST
def trial_activate(request):
    """Manually activate the current user's one-time 7-day free trial.

    This is the ONLY way a trial starts — verify_email() no longer calls
    activate_trial() automatically (see views/auth.py). Distinct failure
    reasons are reported ("not_verified" / "already_used" / "already_paid")
    via a `reason` field alongside distinct HTTP status codes, so the trial
    popup can show the right copy instead of one generic error.

    "already_paid" mirrors context_processors.py::nav_credits()'s
    nav_trial_eligible (trial_manager.can_offer_trial): a user who has ever
    completed a real payment is never offered the trial, even if they never
    activated one — checked here too so a direct POST can't bypass the
    button being hidden client-side.
    """
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse(
            {"status": "error", "message": "Not authenticated.",
             "reason": "not_authenticated"}, status=401)

    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return JsonResponse(
            {"status": "error", "message": "User not found.",
             "reason": "user_not_found"}, status=404)

    if not user.is_verified:
        return JsonResponse(
            {"status": "error",
             "message": "Please verify your email before activating your free trial.",
             "reason": "not_verified"}, status=403)

    if not is_trial_eligible(user):
        return JsonResponse(
            {"status": "error", "message": "You've already used your free trial.",
             "reason": "already_used"}, status=409)

    if has_ever_paid(user_id):
        # Never activated a trial, but has already made a real purchase —
        # the free trial offer is only for someone who hasn't bought
        # anything yet. Checked here too, not just hidden in the UI (see
        # trial_manager.can_offer_trial), so a direct POST to this
        # endpoint can't activate a trial the button was deliberately
        # hidden for.
        return JsonResponse(
            {"status": "error",
             "message": "The free trial is only available before your first purchase.",
             "reason": "already_paid"}, status=409)

    if not activate_trial(user):
        # Lost a race against a concurrent activation (e.g. a double click /
        # two tabs) — activate_trial() re-checks eligibility under a row
        # lock, so this is the same outcome as the check just above,
        # discovered a few milliseconds later.
        return JsonResponse(
            {"status": "error", "message": "You've already used your free trial.",
             "reason": "already_used"}, status=409)

    logger.info("Trial manually activated for user_id=%s", user_id)

    return JsonResponse({
        "status":  "ok",
        "message": "Your 7-day free trial has been activated!",
        "trial_started_at": user.trial_started_at.isoformat(),
        "trial_ends_at":    user.trial_ends_at.isoformat(),
        "trial_days":       TRIAL_DURATION_DAYS,
        "services": [
            {"service": svc, "label": TRIAL_SERVICE_LABELS[svc], "limit": TRIAL_LIMITS[svc]}
            for svc in pricing.SERVICE_KEYS
        ],
    })


# -- Helpers -----------------------------------------------------------------

def _payer_name(payload, user_id):
    name = (payload.get('user_name') or '').strip()
    if name:
        return name[:225]
    user = UserTable.objects.filter(id=user_id).only('user_name').first()
    return user.user_name if user else ''


def _credits_summary(cart):
    """Human-readable summary for Payment.credits, which is a CharField shown
    on the invoice. The legacy flow puts a bare number there; a service
    purchase has no single number, so describe the cart instead."""
    return ", ".join(
        f"{int(qty):,} {pricing.SERVICE_LABELS.get(svc, svc)}"
        for svc, qty in cart.items()
    )[:225]


def _notify(user_id, order, amount_str):
    """Best-effort notification. Credits are already committed, so nothing
    here may raise into the caller."""
    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return

    try:
        from Email_validate_app.utils import create_notification
        create_notification(user_id, 'payment',
                            f"Payment of {order.currency} {amount_str} received — "
                            f"{_credits_summary(order.cart_json)} added",
                            url='/Receipt/')
    except Exception as e:
        logger.error("Service purchase notification failed for user %s: %s", user_id, e)

    if getattr(user, 'notify_payment', True):
        try:
            send_payment_success_email(
                user_name=user.user_name, user_email=user.user_email,
                amount=amount_str, currency=order.currency, order_id=order.order_id,
                payment_time=datetime.utcnow().replace(tzinfo=pytz.UTC),
                extra={'type': 'service_credits', 'cart': order.cart_json},
            )
        except Exception as e:
            logger.error("Service purchase email failed for user %s: %s", user_id, e)
