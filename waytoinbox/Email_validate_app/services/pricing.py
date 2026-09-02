"""Server-side pricing for the service-credit store.

This module is the ONLY authority on what a cart costs. The browser sends
quantities and nothing else — never a price, never a credit amount. Every
entry point (the live quote endpoint, order creation, and post-payment
fulfilment) funnels through quote_cart() so all three can never disagree.

Pricing data lives in the CreditPackage table (seeded by migration 0130), not
in this file, so prices can be corrected by an admin without a deploy.

Two modes:
  tier  — one flat price for the whole band, matched min_qty <= qty <= max_qty
          (max_qty NULL = open-ended top band). Bands are contiguous and
          half-open by construction, so "1-10,000 / 10,001-25,000" resolves
          deterministically: 10,000 -> $39, 10,001 -> $59.
  block — ceil(qty / block_size) * price_usd. Email Marketing bills $2 per
          1,000 emails; Sales Outreach $3 per account.

Money is handled as integer cents end to end. Never float — the existing
`int(price_ * 100)` on a float in views/billing.py:198 is exactly the class of
rounding bug this avoids.
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from Email_validate_app.models import CreditPackage, SERVICE_CHOICES, SERVICE_KEYS

logger = logging.getLogger(__name__)

SERVICE_LABELS = dict(SERVICE_CHOICES)

# Product rule: a selected service line must be purchased at least this many
# credits at a time (0 is still "not selected" and stays free). This is a
# cart-eligibility rule, not a pricing one, so it lives in quote_cart() rather
# than quote_service()/normalize_quantity() — quote_service stays a pure
# per-unit pricing primitive callable at any quantity (admin tools, tests,
# and future per-unit displays all rely on that), and MIN_ORDER_CENTS in
# views/credits.py is a separate, unrelated $1.00 Razorpay floor on the
# order's total price, not a credit-quantity floor.
MIN_QTY_PER_SERVICE = 250

# Unit noun per service, for UI copy ("25,000 emails", "10 accounts").
SERVICE_UNITS = {
    'email_validation': 'emails',
    'email_marketing':  'emails',
    'sales_outreach':   'accounts',
    'reputation':       'domains',
    'header_analysis':  'headers',
    'ip_blocklist':     'IPs',
    'domain_blocklist': 'domains',
}


class PricingError(ValueError):
    """Cart or quantity the pricing table cannot price. Message is safe to
    surface to the user."""


@dataclass(frozen=True)
class QuoteLine:
    service:     str
    label:       str
    quantity:    int
    tier_label:  str
    price_cents: int

    @property
    def price_display(self):
        return f"${self.price_cents / 100:,.2f}"


@dataclass(frozen=True)
class Quote:
    lines:          list = field(default_factory=list)
    subtotal_cents: int = 0
    currency:       str = 'USD'

    @property
    def subtotal_display(self):
        return f"${self.subtotal_cents / 100:,.2f}"

    def as_dict(self):
        return {
            'lines': [
                {
                    'service':     ln.service,
                    'label':       ln.label,
                    'quantity':    ln.quantity,
                    'tier_label':  ln.tier_label,
                    'price_cents': ln.price_cents,
                    'price':       ln.price_display,
                }
                for ln in self.lines
            ],
            'subtotal_cents': self.subtotal_cents,
            'subtotal':       self.subtotal_display,
            'currency':       self.currency,
        }


def _to_cents(price_usd) -> int:
    """Decimal dollars -> integer cents, exactly (no float anywhere)."""
    return int((Decimal(price_usd) * 100).quantize(Decimal('1')))


def normalize_quantity(service: str, raw) -> int:
    """Coerce and validate one quantity. Rejects unknown services, negatives,
    non-integers and malformed input. Returns an int >= 0 (0 = not selected)."""
    if service not in SERVICE_KEYS:
        raise PricingError(f"Unknown service: {service!r}")

    if isinstance(raw, bool):                      # bool is an int subclass
        raise PricingError(f"Invalid quantity for {SERVICE_LABELS[service]}.")
    if isinstance(raw, float) and not raw.is_integer():
        raise PricingError(f"{SERVICE_LABELS[service]} quantity must be a whole number.")

    try:
        qty = int(raw)
    except (TypeError, ValueError):
        raise PricingError(f"Invalid quantity for {SERVICE_LABELS[service]}.")

    if qty < 0:
        raise PricingError(f"{SERVICE_LABELS[service]} quantity cannot be negative.")
    return qty


def quote_service(service: str, qty) -> tuple[int, str]:
    """Price one service at one quantity.

    Returns (price_cents, tier_label). qty=0 returns (0, '') — not selected.
    Raises PricingError for anything unpriceable.
    """
    qty = normalize_quantity(service, qty)
    if qty == 0:
        return 0, ''

    packages = list(
        CreditPackage.objects.filter(service=service, is_active=True).order_by('sort_order')
    )
    if not packages:
        raise PricingError(f"No pricing is configured for {SERVICE_LABELS[service]}.")

    mode = packages[0].mode

    if mode == CreditPackage.MODE_BLOCK:
        pkg = packages[0]
        block = pkg.block_size or 1
        blocks = -(-qty // block)                  # ceil division, integers only
        unit_cents = _to_cents(pkg.price_usd)
        label = f"{blocks} × {block:,}" if block > 1 else f"{qty:,}"
        return blocks * unit_cents, label

    # tier mode
    for pkg in packages:
        lo = pkg.min_qty or 1
        hi = pkg.max_qty                            # None = open-ended
        if qty >= lo and (hi is None or qty <= hi):
            hi_label = f"{hi:,}" if hi is not None else f"{lo:,}+"
            label = f"{lo:,} – {hi_label}" if hi is not None else hi_label
            return _to_cents(pkg.price_usd), label

    raise PricingError(
        f"{qty:,} is above the largest available {SERVICE_LABELS[service]} package. "
        f"Please contact us for volume pricing."
    )


def quote_cart(cart: dict, currency: str = 'USD') -> Quote:
    """Price a whole cart of {service: quantity}.

    Services at quantity 0 are silently dropped (not selected), not errors.
    An empty or all-zero cart yields a Quote with no lines and a 0 subtotal —
    callers decide whether that is an error (order creation) or fine (live
    quote while the user is still choosing).
    """
    if not isinstance(cart, dict):
        raise PricingError("Malformed cart.")

    for key in cart:
        if key not in SERVICE_KEYS:
            raise PricingError(f"Unknown service: {key!r}")

    lines, subtotal = [], 0
    for service in SERVICE_KEYS:                    # stable, spec order
        if service not in cart:
            continue
        qty = normalize_quantity(service, cart[service])
        if qty == 0:
            continue
        if qty < MIN_QTY_PER_SERVICE:
            raise PricingError(
                f"Minimum purchase for {SERVICE_LABELS[service]} is "
                f"{MIN_QTY_PER_SERVICE:,} credits."
            )
        price_cents, tier_label = quote_service(service, qty)
        lines.append(QuoteLine(
            service=service, label=SERVICE_LABELS[service], quantity=qty,
            tier_label=tier_label, price_cents=price_cents,
        ))
        subtotal += price_cents

    return Quote(lines=lines, subtotal_cents=subtotal, currency=currency)


def public_config() -> dict:
    """Everything the browser needs to mirror the pricing maths for an instant
    total. Display-only — the server re-quotes at order time regardless.

    Deliberately does NOT expose any per-credit rate; the UI must never show
    "$0.002 / credit". Only whole-package prices are sent.
    """
    config = {}
    for service in SERVICE_KEYS:
        packages = list(
            CreditPackage.objects.filter(service=service, is_active=True).order_by('sort_order')
        )
        if not packages:
            continue
        entry = {
            'label':   SERVICE_LABELS[service],
            'unit':    SERVICE_UNITS.get(service, 'credits'),
            'mode':    packages[0].mode,
            'min_qty': MIN_QTY_PER_SERVICE,
        }
        if packages[0].mode == CreditPackage.MODE_BLOCK:
            entry['block_size'] = packages[0].block_size or 1
            entry['block_price_cents'] = _to_cents(packages[0].price_usd)
        else:
            entry['tiers'] = [
                {
                    'min': p.min_qty or 1,
                    'max': p.max_qty,               # None = open-ended
                    'price_cents': _to_cents(p.price_usd),
                }
                for p in packages
            ]
            entry['max_qty'] = packages[-1].max_qty  # None = unbounded
        config[service] = entry
    return config


def validate_pricing_table() -> list[str]:
    """Sanity-check the seeded ladders. Returns a list of human-readable
    warnings; an empty list means the table is well formed.

    Hard problems (gaps, overlaps, descending bands) and soft ones
    (non-monotonic prices — buying more costing less) are both reported. This
    is a diagnostic for admins, never an auto-corrector: the supplied ladders
    contain deliberate anomalies that must be preserved until a human decides
    otherwise.
    """
    warnings = []
    for service in SERVICE_KEYS:
        packages = list(
            CreditPackage.objects.filter(service=service, is_active=True).order_by('sort_order')
        )
        if not packages:
            warnings.append(f"{service}: no active packages configured")
            continue
        if packages[0].mode == CreditPackage.MODE_BLOCK:
            if not packages[0].block_size:
                warnings.append(f"{service}: block mode with no block_size")
            continue

        prev_max, prev_price = 0, None
        for p in packages:
            lo, hi = p.min_qty or 1, p.max_qty
            if lo != prev_max + 1:
                warnings.append(
                    f"{service}: band starting {lo:,} leaves a gap/overlap "
                    f"after {prev_max:,}"
                )
            price = _to_cents(p.price_usd)
            if prev_price is not None and price < prev_price:
                warnings.append(
                    f"{service}: {lo:,}–{hi if hi else 'open'} costs "
                    f"${price/100:,.2f} — less than the smaller band above it "
                    f"(${prev_price/100:,.2f}); the smaller band is dominated"
                )
            prev_max = hi if hi is not None else prev_max
            prev_price = price
            if hi is None:
                break
    return warnings
