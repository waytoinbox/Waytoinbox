"""Customer-side coupon validation and redemption.

Not to be confused with services/admin/coupon_service.py, which is the admin
CRUD for creating and editing coupons. This module is the other half: deciding
whether a shopper may use a code, what it is worth, and recording the fact
when they do.

Before this, Coupon had admin CRUD and nothing else — `used_count` was never
incremented anywhere in the codebase and no redemption was ever recorded.

Two rules shape everything here:

  * Money is integer cents, matching services/pricing.py. Coupon.discount_value
    and min_order_amount are Decimal dollars in the DB and are converted once,
    exactly, on the way in. No float ever touches a price.

  * Validation is advisory; redemption is authoritative. validate_coupon() is
    called at quote time and again at order time, but it takes no locks and
    changes nothing. redeem_coupon() is the only function that writes, and it
    re-checks every limit while holding a row lock — because between the quote
    and the payment, someone else may have used the last remaining use.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


class CouponError(ValueError):
    """A coupon that cannot be applied. The message is safe to show the user."""


def _to_cents(amount) -> int:
    """Decimal dollars -> integer cents, exactly."""
    return int((Decimal(amount) * 100).quantize(Decimal('1')))


def normalize_code(code) -> str:
    return (code or '').strip().upper()


def parse_applicable_services(coupon):
    """Blank means every service. Otherwise a comma-separated list of service
    keys. Unknown keys are dropped rather than raising, so a typo in the admin
    narrows the coupon instead of breaking checkout."""
    from Email_validate_app.models import SERVICE_KEYS

    raw = (coupon.applicable_services or '').strip()
    if not raw:
        return None                       # None = unrestricted
    keys = {part.strip() for part in raw.split(',') if part.strip()}
    valid = {k for k in keys if k in SERVICE_KEYS}
    if keys - valid:
        logger.warning("Coupon %s lists unknown services: %s",
                       coupon.code, sorted(keys - valid))
    return valid or None


def eligible_subtotal_cents(coupon, quote) -> int:
    """How much of this quote the coupon is allowed to discount.

    An unrestricted coupon sees the whole subtotal. A restricted one sees only
    the lines for services it names — so a 20%-off-validation code applied to a
    cart of validation + outreach discounts the validation half only, rather
    than the whole basket.
    """
    allowed = parse_applicable_services(coupon)
    if allowed is None:
        return quote.subtotal_cents
    return sum(line.price_cents for line in quote.lines if line.service in allowed)


def compute_discount_cents(coupon, eligible_cents) -> int:
    """What the coupon is worth against `eligible_cents`.

    Never exceeds the eligible amount, so a total can never go negative and a
    $50 fixed coupon on a $30 basket is worth $30, not $50.
    """
    if eligible_cents <= 0:
        return 0

    if coupon.discount_type == 'percentage':
        pct = Decimal(coupon.discount_value)
        # Quantize once, on cents, so 33% of $59.00 is a whole number of cents.
        discount = int((Decimal(eligible_cents) * pct / Decimal(100)).quantize(Decimal('1')))
    elif coupon.discount_type == 'fixed_amount':
        discount = _to_cents(coupon.discount_value)
    else:
        # 'fixed_credits' grants credits rather than reducing a price. There is
        # no existing implementation of it anywhere (nothing has ever redeemed
        # a coupon), and with seven services "N free credits" does not say
        # which service it means. Rather than invent that, it is rejected in
        # validate_coupon() with a clear reason and is worth nothing here.
        return 0

    return max(0, min(discount, eligible_cents))


def validate_coupon(code, user, cart_total, quote=None):
    """Decide whether `user` may use `code` on a cart worth `cart_total` cents.

    Returns (ok, discount_cents, reason). `reason` is a user-facing sentence:
    on success it describes the discount, on failure it says why not.

    Read-only and lock-free — safe to call on every keystroke-debounced quote.
    It is deliberately NOT sufficient on its own: redeem_coupon() re-checks
    everything under a lock, because this answer can go stale.

    `quote` is optional so the documented three-argument signature holds; it is
    needed only to honour applicable_services, and without it the coupon is
    treated as applying to the whole cart.
    """
    from Email_validate_app.models import Coupon, CouponRedemption

    code = normalize_code(code)
    if not code:
        return False, 0, ''

    try:
        coupon = Coupon.objects.get(code=code)
    except Coupon.DoesNotExist:
        return False, 0, "That promo code is not valid."

    if not coupon.is_active:
        return False, 0, "That promo code is no longer active."

    now = timezone.now()
    if coupon.valid_from and now < coupon.valid_from:
        return False, 0, "That promo code is not active yet."
    if coupon.valid_until and now > coupon.valid_until:
        return False, 0, "That promo code has expired."

    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return False, 0, "That promo code has reached its usage limit."

    if user is not None and coupon.per_user_limit is not None:
        used_by_user = CouponRedemption.objects.filter(
            coupon=coupon, user=user).count()
        if used_by_user >= coupon.per_user_limit:
            return False, 0, "You have already used that promo code."

    min_cents = _to_cents(coupon.min_order_amount or 0)
    if min_cents and cart_total < min_cents:
        return False, 0, (f"That promo code needs a minimum order of "
                          f"${min_cents / 100:,.2f}.")

    if coupon.discount_type == 'fixed_credits':
        # See compute_discount_cents(): deliberately unsupported here rather
        # than guessed at.
        return False, 0, "That promo code cannot be used on credit purchases."

    eligible = eligible_subtotal_cents(coupon, quote) if quote is not None else cart_total
    if eligible <= 0:
        return False, 0, "That promo code does not apply to the services you selected."

    discount = compute_discount_cents(coupon, eligible)
    if discount <= 0:
        return False, 0, "That promo code does not apply to the services you selected."

    return True, discount, f"Promo code applied — ${discount / 100:,.2f} off."


def redeem_coupon(order, payment, *, user_id):
    """Record a redemption for a paid order. Call inside the fulfilment
    transaction, after the payment row exists.

    Re-validates the usage limits while holding a row lock on the coupon, then
    writes the CouponRedemption and increments used_count with an F() update.
    Returns the CouponRedemption, or None if no coupon was used or the limits
    had been exhausted since the order was created.

    On exhaustion the purchase still completes. The customer was already
    charged the discounted amount by Razorpay at order-creation time, so
    refusing here would mean taking their money and withholding the credits.
    Instead the redemption is skipped — which keeps used_count from exceeding
    max_uses, as required — and the discrepancy is logged at error level for
    finance to review.

    Lock order is ServiceOrder -> Coupon -> ServiceCredit -> CurrentCredits.
    Every writer takes them in that order; do not reorder.
    """
    from Email_validate_app.models import Coupon, CouponRedemption

    if not order.coupon_id or not order.discount_cents:
        return None

    try:
        coupon = Coupon.objects.select_for_update().get(pk=order.coupon_id)
    except Coupon.DoesNotExist:
        logger.error("Order %s referenced coupon %s which no longer exists; "
                     "discount of %s cents was already applied at checkout.",
                     order.order_id, order.coupon_id, order.discount_cents)
        return None

    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        logger.error(
            "Coupon %s hit its %s-use limit between order %s being created and "
            "being paid. The customer was already charged the discounted "
            "amount (%s cents off), so the purchase completed, but no "
            "redemption was recorded and used_count was not incremented. "
            "Review this order.",
            coupon.code, coupon.max_uses, order.order_id, order.discount_cents)
        return None

    if coupon.per_user_limit is not None:
        used_by_user = CouponRedemption.objects.filter(
            coupon=coupon, user_id=user_id).count()
        if used_by_user >= coupon.per_user_limit:
            logger.error(
                "User %s hit the %s-per-user limit on coupon %s between order "
                "%s being created and being paid. Purchase completed; no "
                "redemption recorded. Review this order.",
                user_id, coupon.per_user_limit, coupon.code, order.order_id)
            return None

    redemption = CouponRedemption.objects.create(
        coupon=coupon, user_id=user_id, payment=payment,
        discount_applied=Decimal(order.discount_cents) / Decimal(100),
    )

    # F() so the increment is computed by the database, not from a value this
    # process read earlier.
    Coupon.objects.filter(pk=coupon.pk).update(used_count=F('used_count') + 1)

    logger.info("Coupon %s redeemed by user %s on order %s (%s cents off)",
                coupon.code, user_id, order.order_id, order.discount_cents)
    return redemption
