"""Tests for the service-based credit system (Phase 1).

Covers pricing, the new ServiceCredit wallets, and — most importantly — the
legacy drain-down fallback, which is the piece that must never multiply or
lose an existing customer's balance.
"""
from decimal import Decimal

from django.test import TestCase

from Email_validate_app.models import (
    UserTable, CurrentCredits, CreditAuditLog, CreditPackage, ServiceCredit,
)
from Email_validate_app.services.pricing import (
    quote_service, quote_cart, public_config, validate_pricing_table, PricingError,
    MIN_QTY_PER_SERVICE, SERVICE_KEYS as PRICING_SERVICE_KEYS,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, deduct_service_credits, ensure_service_credits,
    refund_service_credits, get_service_balance, get_effective_balance,
    get_all_service_balances, InsufficientCredits, expire_subscription_credits,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Credit Test', user_email=email, password='StrongPass123!')


# ── Pricing ───────────────────────────────────────────────────────────────

class PricingTierTests(TestCase):
    """Boundaries are the whole game here — the source spec's ranges overlap
    ("1-10,000 / 10,000-25,000") and are resolved as 1-10,000 / 10,001-25,000."""

    def test_email_validation_boundaries(self):
        for qty, cents in [
            (1, 3900), (10_000, 3900), (10_001, 5900), (25_000, 5900),
            (25_001, 8900), (50_000, 8900), (50_001, 14900),
            (25_000_001, 849900), (50_000_000, 849900),
        ]:
            self.assertEqual(quote_service('email_validation', qty)[0], cents, qty)

    def test_monitor_ladder_shared_by_three_services(self):
        for service in ('ip_blocklist', 'domain_blocklist', 'reputation'):
            for qty, cents in [
                (1, 10000), (50, 10000), (51, 20000), (100, 20000),
                (101, 40000), (200, 40000), (201, 50000), (300, 50000),
                (301, 50000), (500, 50000), (501, 100000), (1_000, 100000),
                (1_001, 200000), (2_000, 200000), (2_001, 1000000),
            ]:
                self.assertEqual(quote_service(service, qty)[0], cents,
                                 f"{service} @ {qty}")

    def test_header_analysis_including_the_dominated_band(self):
        # 51-100 costs $40 while 101-200 costs $35 — buying more costs less.
        # Supplied as-is and deliberately preserved, not auto-corrected.
        for qty, cents in [(50, 2000), (51, 4000), (100, 4000),
                           (101, 3500), (200, 3500), (201, 4500), (2_001, 100000)]:
            self.assertEqual(quote_service('header_analysis', qty)[0], cents, qty)

    def test_open_ended_top_band_has_no_upper_limit(self):
        self.assertEqual(quote_service('email_validation', 999_999_999)[0], 849900)
        self.assertEqual(quote_service('ip_blocklist', 5_000_000)[0], 1000000)


class PricingBlockTests(TestCase):
    def test_email_marketing_is_two_dollars_per_thousand(self):
        for qty, cents in [(1, 200), (999, 200), (1_000, 200), (1_001, 400),
                           (2_000, 400), (2_001, 600), (5_000, 1000), (10_000, 2000)]:
            self.assertEqual(quote_service('email_marketing', qty)[0], cents, qty)

    def test_sales_outreach_is_three_dollars_per_account(self):
        for qty, cents in [(1, 300), (5, 1500), (10, 3000), (100, 30000)]:
            self.assertEqual(quote_service('sales_outreach', qty)[0], cents, qty)


class PricingValidationTests(TestCase):
    def test_zero_is_free_and_excluded_from_the_cart(self):
        self.assertEqual(quote_service('email_validation', 0)[0], 0)
        q = quote_cart({'email_validation': 0, 'sales_outreach': 0})
        self.assertEqual(q.lines, [])
        self.assertEqual(q.subtotal_cents, 0)

    def test_rejects_bad_quantities(self):
        for bad in (-1, -1000, 2.5, 'abc', None, True, [1]):
            with self.assertRaises(PricingError):
                quote_service('email_validation', bad)

    def test_rejects_unknown_service(self):
        with self.assertRaises(PricingError):
            quote_service('warp_drive', 10)
        with self.assertRaises(PricingError):
            quote_cart({'warp_drive': 10})

    def test_spec_cart_example_totals_correctly(self):
        # sales_outreach bumped to the 250-credit minimum (below it, the cart
        # would be rejected outright — see PricingMinimumQuantityTests).
        q = quote_cart({'email_validation': 25_000,
                        'email_marketing': 5_000,
                        'sales_outreach': 250})
        self.assertEqual(q.subtotal_cents, 81900)   # $59 + $10 + $750
        self.assertEqual(q.subtotal_display, '$819.00')
        self.assertEqual(len(q.lines), 3)

    def test_public_config_never_exposes_a_per_credit_rate(self):
        cfg = public_config()
        self.assertEqual(len(cfg), 7)
        blob = str(cfg)
        for leaked in ('per_credit', 'unit_price', 'rate'):
            self.assertNotIn(leaked, blob)

    def test_seeded_ladders_are_gapless(self):
        gaps = [w for w in validate_pricing_table() if 'gap/overlap' in w]
        self.assertEqual(gaps, [], f"pricing ladders have gaps: {gaps}")


# ── Phase 7: minimum purchase quantity ──────────────────────────────────────

class PricingMinimumQuantityTests(TestCase):
    """A selected service line must be purchased at MIN_QTY_PER_SERVICE (250)
    or more. 0 is unaffected — it means "not selected", not "buy zero".

    This is a quote_cart() rule, not a quote_service() one: quote_service
    stays a pure per-unit pricing primitive (PricingTierTests/PricingBlockTests
    above price quantities well under 250 directly through it), while
    quote_cart is the single funnel every real checkout entry point
    (subscription_quote / subscription_order) uses.
    """

    def test_249_is_rejected_for_every_service(self):
        for service in PRICING_SERVICE_KEYS:
            with self.assertRaises(PricingError, msg=service):
                quote_cart({service: MIN_QTY_PER_SERVICE - 1})

    def test_250_is_accepted_for_every_service(self):
        for service in PRICING_SERVICE_KEYS:
            q = quote_cart({service: MIN_QTY_PER_SERVICE})
            self.assertEqual(len(q.lines), 1, service)
            self.assertEqual(q.lines[0].quantity, MIN_QTY_PER_SERVICE, service)

    def test_251_is_accepted(self):
        q = quote_cart({'email_validation': MIN_QTY_PER_SERVICE + 1})
        self.assertEqual(q.lines[0].quantity, 251)

    def test_zero_is_still_not_selected_not_a_minimum_violation(self):
        q = quote_cart({'email_validation': 0, 'sales_outreach': 0})
        self.assertEqual(q.lines, [])
        self.assertEqual(q.subtotal_cents, 0)

    def test_one_bad_line_rejects_the_whole_cart(self):
        with self.assertRaises(PricingError):
            quote_cart({'email_validation': 25_000, 'sales_outreach': 1})

    def test_error_message_names_the_service_and_the_floor(self):
        with self.assertRaises(PricingError) as ctx:
            quote_cart({'sales_outreach': 100})
        msg = str(ctx.exception)
        self.assertIn('Sales Outreach', msg)
        self.assertIn('250', msg)

    def test_quote_service_itself_is_unaffected_by_the_cart_minimum(self):
        # The per-unit pricing primitive still prices any positive quantity —
        # only quote_cart (the checkout funnel) enforces the 250 floor.
        price_cents, _ = quote_service('email_validation', 1)
        self.assertEqual(price_cents, 3900)

    def test_public_config_exposes_the_minimum_for_every_service(self):
        cfg = public_config()
        for service, entry in cfg.items():
            self.assertEqual(entry['min_qty'], MIN_QTY_PER_SERVICE, service)


# ── New wallets ───────────────────────────────────────────────────────────

class ServiceCreditBasicTests(TestCase):
    def setUp(self):
        self.user = make_user('svc-basic@example.com')

    def test_add_then_spend(self):
        add_service_credits(self.user.id, 'email_validation', 1000, ref_id='o1')
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 1000)

        deduct_service_credits(self.user.id, 'email_validation', 250,
                               ref_type='validation')
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 750)

        row = ServiceCredit.objects.get(user=self.user, service='email_validation')
        self.assertEqual(row.total_purchased, 1000)
        self.assertEqual(row.total_used, 250)

    def test_insufficient_raises_and_writes_nothing(self):
        add_service_credits(self.user.id, 'reputation', 5)
        before_logs = CreditAuditLog.objects.count()

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'reputation', 10)

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 5)
        self.assertEqual(CreditAuditLog.objects.count(), before_logs)

    def test_insufficient_is_a_valueerror_for_existing_handlers(self):
        # Existing deduction sites all catch ValueError; the new exception must
        # remain catchable by them.
        with self.assertRaises(ValueError):
            deduct_service_credits(self.user.id, 'sales_outreach', 1)

    def test_refund_returns_to_the_new_wallet(self):
        add_service_credits(self.user.id, 'sales_outreach', 3)
        deduct_service_credits(self.user.id, 'sales_outreach', 3)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)
        refund_service_credits(self.user.id, 'sales_outreach', 1, ref_type='so_account')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)

    def test_ensure_preflight_blocks_before_any_work(self):
        add_service_credits(self.user.id, 'email_validation', 500)
        with self.assertRaises(InsufficientCredits) as ctx:
            ensure_service_credits(self.user.id, 'email_validation', 750)
        self.assertEqual(ctx.exception.available, 500)
        self.assertEqual(ctx.exception.needed, 750)
        # Preflight is read-only.
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 500)

    def test_sales_outreach_has_no_legacy_fallback(self):
        CurrentCredits.objects.create(user=self.user, ac_current_credits=500,
                                      vc_current_credits=500, cc_current_credits=500)
        self.assertEqual(get_effective_balance(self.user.id, 'sales_outreach'), 0)
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'sales_outreach', 1)


# ── Legacy drain-down — the critical behaviour ────────────────────────────

class LegacyFallbackTests(TestCase):
    def setUp(self):
        self.user = make_user('svc-legacy@example.com')

    def test_spec_split_example(self):
        """new=20, legacy vc=100, request 50 -> new=0, legacy=70."""
        add_service_credits(self.user.id, 'email_validation', 20)
        CurrentCredits.objects.create(user=self.user, vc_current_credits=100)

        deduct_service_credits(self.user.id, 'email_validation', 50,
                               ref_type='validation')

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        cc = CurrentCredits.objects.get(user=self.user)
        self.assertEqual(cc.vc_current_credits, 70)

    def test_split_spend_writes_one_audit_row_per_pool(self):
        add_service_credits(self.user.id, 'email_validation', 20)
        CurrentCredits.objects.create(user=self.user, vc_current_credits=100)
        CreditAuditLog.objects.all().delete()

        deduct_service_credits(self.user.id, 'email_validation', 50,
                               ref_type='validation')

        debits = CreditAuditLog.objects.filter(entry_type='debit')
        self.assertEqual(debits.count(), 2)
        self.assertEqual(debits.get(credit_type='email_validation').amount, -20)
        self.assertEqual(debits.get(credit_type='vc').amount, -30)

    def test_legacy_ac_is_one_shared_pool_not_four(self):
        """THE high-value test. 100 legacy AC is shared by four services. The
        user must be able to spend exactly 100 in total across all of them —
        never 400 — and the pool must land on exactly 0.
        """
        CurrentCredits.objects.create(user=self.user, ac_current_credits=100)

        for service in ('reputation', 'header_analysis',
                        'ip_blocklist', 'domain_blocklist'):
            deduct_service_credits(self.user.id, service, 25, ref_type='ip_check')

        cc = CurrentCredits.objects.get(user=self.user)
        self.assertEqual(cc.ac_current_credits, 0)
        self.assertEqual(cc.ac_used_credits, 100)

        # The 101st credit must not exist, on any of the four services.
        for service in ('reputation', 'header_analysis',
                        'ip_blocklist', 'domain_blocklist'):
            with self.assertRaises(InsufficientCredits):
                deduct_service_credits(self.user.id, service, 1)

    def test_spending_one_analysis_service_reduces_what_the_others_see(self):
        CurrentCredits.objects.create(user=self.user, ac_current_credits=100)
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 100)

        deduct_service_credits(self.user.id, 'ip_blocklist', 40, ref_type='ip_check')

        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 60)
        self.assertEqual(get_effective_balance(self.user.id, 'header_analysis'), 60)

    def test_new_wallet_is_spent_before_legacy(self):
        add_service_credits(self.user.id, 'reputation', 10)
        CurrentCredits.objects.create(user=self.user, ac_current_credits=100)

        deduct_service_credits(self.user.id, 'reputation', 4, ref_type='ip_check')

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 6)
        self.assertEqual(CurrentCredits.objects.get(user=self.user).ac_current_credits, 100)

    def test_all_balances_reports_shared_pool_separately(self):
        CurrentCredits.objects.create(user=self.user, ac_current_credits=100)
        add_service_credits(self.user.id, 'reputation', 5)

        data = get_all_service_balances(self.user.id)

        self.assertEqual(data['legacy_shared']['ac'], 100)
        self.assertEqual(data['services']['reputation']['new'], 5)
        self.assertEqual(data['services']['reputation']['effective'], 105)
        # Every analysis service reflects the SAME legacy pool.
        for service in ('reputation', 'header_analysis',
                        'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(data['services'][service]['legacy'], 100)
        self.assertEqual(data['services']['sales_outreach']['legacy'], 0)


# ── Expiry must never touch the new wallets ───────────────────────────────

class ExpiryIsolationTests(TestCase):
    """Load-bearing regression: purchased service credits never expire.

    ServiceCredit lives in its own table precisely so the nightly expiry job
    cannot reach it. If someone ever 'helpfully' extends that job to the new
    wallets, this fails.
    """

    def test_expiry_leaves_service_credits_untouched(self):
        user = make_user('svc-expiry@example.com')
        add_service_credits(user.id, 'reputation', 40)
        add_service_credits(user.id, 'email_validation', 900)
        CurrentCredits.objects.create(user=user, ac_current_credits=25,
                                      cc_current_credits=15)

        class _FakeSub:
            order_id, pk, subs_plan = 'ORD-EXPIRY-TEST', 1, 'Classic'

        expire_subscription_credits(user.id, _FakeSub())

        self.assertEqual(get_service_balance(user.id, 'reputation'), 40)
        self.assertEqual(get_service_balance(user.id, 'email_validation'), 900)
