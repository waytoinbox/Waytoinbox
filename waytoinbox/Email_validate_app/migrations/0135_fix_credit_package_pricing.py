"""Replace the CreditPackage pricing catalogue with the correct per-credit
rates for all 7 services.

Two things this migration fixes:

1. quote_service() previously treated tier-mode price_usd as a FLAT total
   for the whole band; the actual product pricing is a PER-CREDIT rate
   (services/pricing.py now multiplies qty * price_usd instead of
   returning price_usd directly). The old 0130 seed values were flat
   totals under the old interpretation and are wrong under the new one, so
   every tier-mode row for the 5 tiered services is replaced outright.

2. Whatever CreditPackage data existed in a given database (stale rows from
   0130, or none at all) is superseded unconditionally: every currently
   active row for these 7 services is deactivated first, then the correct
   rows are inserted fresh. This is deliberately NOT the same idempotent
   get_or_create pattern 0130 used — that pattern only fills in gaps and
   never corrects existing wrong values, which is exactly the bug being
   fixed here. Deactivating rather than deleting preserves history (an
   admin can still see what used to be priced) without leaving stale rows
   active and eligible to be matched by quote_service().

This migration does not read from or write to any user balance table
(CurrentCredits / ServiceCredit / etc.) — pricing catalogue only.
"""
from decimal import Decimal

from django.db import migrations


# (min_qty, max_qty, price_usd_per_credit). max_qty=None is the open-ended
# top band. Bands are contiguous by construction (each min_qty is the
# previous band's max_qty + 1), so there are no gaps or overlaps.
EMAIL_VALIDATION_TIERS = [
    (1,           10_000,      '0.0039'),
    (10_001,      25_000,      '0.00236'),
    (25_001,      50_000,      '0.00178'),
    (50_001,      100_000,     '0.00149'),
    (100_001,     500_000,     '0.00059'),
    (500_001,     1_000_000,   '0.00044'),
    (1_000_001,   2_000_000,   '0.00039'),
    (2_000_001,   3_000_000,   '0.00036'),
    (3_000_001,   4_000_000,   '0.00034'),
    (4_000_001,   5_000_000,   '0.00031'),
    (5_000_001,   10_000_000,  '0.00025'),
    (10_000_001,  25_000_000,  '0.00019'),
    (25_000_001,  None,        '0.00016'),
]

# Shared verbatim by IP Blocklist, Domain Blocklist and Reputation Analysis.
MONITOR_TIERS = [
    (1,   300,  '2'),
    (301, None, '1'),
]

HEADER_ANALYSIS_TIERS = [
    (1,   100,  '0.40'),
    (101, 200,  '0.175'),
    (201, 300,  '0.15'),
    (301, None, '0.10'),
]

TIER_LADDERS = {
    'email_validation': EMAIL_VALIDATION_TIERS,
    'ip_blocklist':     MONITOR_TIERS,
    'domain_blocklist': MONITOR_TIERS,
    'reputation':       MONITOR_TIERS,
    'header_analysis':  HEADER_ANALYSIS_TIERS,
}

# (service, block_size, price_usd_per_credit). Charged as
# ceil(qty / block_size) * price_usd; block_size=1 is plain per-credit
# pricing. Sales Outreach is numerically unchanged from the 0130 seed
# ($3/account); Email Marketing changes from "$2 per 1,000-block" to a
# plain $0.002-per-credit rate (block_size 1000 -> 1), which is the same
# rate at exact multiples of 1,000 but no longer rounds any purchase under
# 1,000 up to a full block's price.
BLOCK_PACKAGES = [
    ('email_marketing', 1, '0.002'),
    ('sales_outreach',  1, '3'),
]

REPRICED_SERVICES = list(TIER_LADDERS.keys()) + [svc for svc, _, _ in BLOCK_PACKAGES]


def fix_pricing(apps, schema_editor):
    CreditPackage = apps.get_model('Email_validate_app', 'CreditPackage')

    CreditPackage.objects.filter(
        service__in=REPRICED_SERVICES, is_active=True,
    ).update(is_active=False)

    for service, ladder in TIER_LADDERS.items():
        for order, (min_qty, max_qty, price) in enumerate(ladder):
            CreditPackage.objects.create(
                service=service, mode='tier', min_qty=min_qty, max_qty=max_qty,
                block_size=None, price_usd=Decimal(price),
                is_active=True, sort_order=order,
            )

    for order, (service, block_size, price) in enumerate(BLOCK_PACKAGES):
        CreditPackage.objects.create(
            service=service, mode='block', min_qty=None, max_qty=None,
            block_size=block_size, price_usd=Decimal(price),
            is_active=True, sort_order=order,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0134_widen_credit_package_price_precision'),
    ]

    operations = [
        # Reverse is a deliberate no-op, same as 0130: rolling back the
        # schema should never silently delete or reactivate rows in a
        # pricing catalogue an admin may have since edited, and there is no
        # reliable way to tell "a row this migration created" apart from
        # "a row an admin added afterward with the same shape".
        migrations.RunPython(fix_pricing, migrations.RunPython.noop),
    ]
