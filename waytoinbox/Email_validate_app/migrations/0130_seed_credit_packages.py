"""Seed the CreditPackage pricing catalogue.

This migration ONLY inserts pricing rows. It reads nothing from, and writes
nothing to, any user balance table (CurrentCredits / TotalCredits /
UsedCredits / ServiceCredit) — per the hard requirement that no migration may
modify existing customer credit balances.

Idempotent: uses get_or_create keyed on the natural key, so re-running is a
no-op AND any price an admin has since corrected through the admin UI is left
untouched rather than being reset to the seeded value.

Reverse is a deliberate no-op: rolling back the schema should never silently
delete a pricing catalogue an admin may have edited.
"""
from decimal import Decimal

from django.db import migrations


# Tier ladders are (min_qty, max_qty, price_usd). max_qty=None is the
# open-ended top band. Boundaries are half-open by construction — each band's
# min_qty is the previous band's max_qty + 1 — which resolves the overlaps in
# the source spec ("1–10,000 / 10,000–25,000") deterministically.
EMAIL_VALIDATION_TIERS = [
    (1,           10_000,      '39'),
    (10_001,      25_000,      '59'),
    (25_001,      50_000,      '89'),
    (50_001,      100_000,     '149'),
    (100_001,     500_000,     '299'),
    (500_001,     1_000_000,   '449'),
    (1_000_001,   2_000_000,   '799'),
    (2_000_001,   3_000_000,   '1099'),
    (3_000_001,   4_000_000,   '1399'),
    (4_000_001,   5_000_000,   '1599'),
    (5_000_001,   10_000_000,  '2599'),
    (10_000_001,  25_000_000,  '4999'),
    (25_000_001,  None,        '8499'),
]

# Used verbatim by IP Blocklist, Domain Blocklist and Reputation Analysis.
#
# NOTE: 201–300 and 301–500 are both $500. That is exactly as supplied and is
# intentionally NOT "corrected" here — it means 201–300 is dominated by
# 301–500. Prices live in the DB precisely so this can be changed by an admin
# without a deploy.
MONITOR_TIERS = [
    (1,     50,     '100'),
    (51,    100,    '200'),
    (101,   200,    '400'),
    (201,   300,    '500'),
    (301,   500,    '500'),
    (501,   1_000,  '1000'),
    (1_001, 2_000,  '2000'),
    (2_001, None,   '10000'),
]

# NOTE: 51–100 is $40 but 101–200 is $35 — buying more costs less, so 51–100
# is dominated. Again, supplied as-is and deliberately not auto-corrected.
HEADER_ANALYSIS_TIERS = [
    (1,     50,     '20'),
    (51,    100,    '40'),
    (101,   200,    '35'),
    (201,   300,    '45'),
    (301,   500,    '50'),
    (501,   1_000,  '100'),
    (1_001, 2_000,  '200'),
    (2_001, None,   '1000'),
]

TIER_LADDERS = {
    'email_validation': EMAIL_VALIDATION_TIERS,
    'ip_blocklist':     MONITOR_TIERS,
    'domain_blocklist': MONITOR_TIERS,
    'reputation':       MONITOR_TIERS,
    'header_analysis':  HEADER_ANALYSIS_TIERS,
}

# (service, block_size, price_usd) — price is charged per block, i.e.
# ceil(qty / block_size) * price_usd.
BLOCK_PACKAGES = [
    ('email_marketing', 1000, '2'),   # $2 per 1,000 emails
    ('sales_outreach',  1,    '3'),   # $3 per account
]


def seed_packages(apps, schema_editor):
    CreditPackage = apps.get_model('Email_validate_app', 'CreditPackage')

    for service, ladder in TIER_LADDERS.items():
        for order, (min_qty, max_qty, price) in enumerate(ladder):
            CreditPackage.objects.get_or_create(
                service=service, mode='tier', min_qty=min_qty, max_qty=max_qty,
                defaults={
                    'price_usd':  Decimal(price),
                    'block_size': None,
                    'is_active':  True,
                    'sort_order': order,
                },
            )

    for order, (service, block_size, price) in enumerate(BLOCK_PACKAGES):
        CreditPackage.objects.get_or_create(
            service=service, mode='block', block_size=block_size,
            defaults={
                'price_usd':  Decimal(price),
                'min_qty':    None,
                'max_qty':    None,
                'is_active':  True,
                'sort_order': order,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0129_service_credits'),
    ]

    operations = [
        migrations.RunPython(seed_packages, migrations.RunPython.noop),
    ]
