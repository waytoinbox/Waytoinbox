"""Tests for Phase 5: coupon validation, discount arithmetic and redemption.

Split into three concerns:

  * validate_coupon() — every rejection reason, and the discount maths.
  * the checkout endpoints — that a discount reaches Razorpay, that the browser
    cannot forge one, and that used_count moves exactly once and only at the
    right moment.
  * concurrency — that two simultaneous checkouts cannot overrun max_uses.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.utils import timezone
from razorpay.errors import SignatureVerificationError

from Email_validate_app.models import (
    UserTable, Coupon, CouponRedemption, ServiceOrder, Payment, ServiceCredit,
    CurrentCredits,
)
from Email_validate_app.services import coupon_service
from Email_validate_app.services.pricing import quote_cart
from Email_validate_app.services.credit_manager import get_service_balance


def make_user(email):
    # subscription_order() now requires is_verified=True (unverified sessions
    # can browse but not purchase) -- this file is about coupon/checkout
    # logic, not verification, so its users are verified by default.
    user = UserTable.objects.create_user(
        user_name='Coupon Test', user_email=email, password='StrongPass123!')
    user.is_verified = True
    user.save(update_fields=['is_verified'])
    return user


def make_coupon(code='SAVE20', **kwargs):
    defaults = dict(
        discount_type='percentage',
        discount_value=Decimal('20'),
        valid_from=timezone.now() - timedelta(days=1),
        is_active=True,
    )
    defaults.update(kwargs)
    return Coupon.objects.create(code=code, **defaults)


def fake_razorpay(order_id='order_TEST123', raise_signature=False):
    client = MagicMock()
    client.order.create.return_value = {'id': order_id, 'amount': 0, 'currency': 'USD'}
    if raise_signature:
        client.utility.verify_payment_signature.side_effect = SignatureVerificationError('bad')
    else:
        client.utility.verify_payment_signature.return_value = None
    client.payment.fetch.return_value = {
        'email': 'payer@example.com', 'contact': '+10000000000', 'method': 'card'}
    return client


# subscription_order() accepts any nonempty subset of the 7 services -- a
# full cart is not required. Same fixture as test_service_checkout.py, used
# here purely as a convenient, deterministic multi-service cart for coupon
# tests: every service at exactly its minimum quantity, totalling 1530 cents.
FULL_CART_AT_MINIMUM = {
    'email_validation': 1000,
    'email_marketing':  1000,
    'sales_outreach':   1,
    'reputation':        1,
    'header_analysis':   1,
    'ip_blocklist':      1,
    'domain_blocklist':  1,
}


# ── validate_coupon ───────────────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class CouponValidationTests(TestCase):

    def setUp(self):
        self.user = make_user('validate@example.com')
        # $59 of email validation.
        self.quote = quote_cart({'email_validation': 25_000})

    def _validate(self, code, quote=None):
        q = quote or self.quote
        return coupon_service.validate_coupon(code, self.user, q.subtotal_cents, quote=q)

    # -- valid --------------------------------------------------------------

    def test_percentage_discount(self):
        make_coupon('SAVE20', discount_type='percentage', discount_value=Decimal('20'))
        ok, discount, reason = self._validate('SAVE20')
        self.assertTrue(ok, reason)
        self.assertEqual(discount, 1180)                  # 20% of $59.00
        self.assertEqual(self.quote.subtotal_cents - discount, 4720)

    def test_fixed_amount_discount(self):
        make_coupon('TENOFF', discount_type='fixed_amount', discount_value=Decimal('10'))
        ok, discount, _ = self._validate('TENOFF')
        self.assertTrue(ok)
        self.assertEqual(discount, 1000)                  # $10.00
        self.assertEqual(self.quote.subtotal_cents - discount, 4900)

    def test_code_is_case_and_whitespace_insensitive(self):
        make_coupon('SAVE20')
        for variant in ('save20', '  SAVE20  ', 'SaVe20'):
            ok, discount, _ = self._validate(variant)
            self.assertTrue(ok, variant)
            self.assertEqual(discount, 1180)

    def test_percentage_rounds_to_whole_cents(self):
        # 33% of $59.00 = $19.47 exactly; must not produce a fraction of a cent.
        make_coupon('THIRTY3', discount_type='percentage', discount_value=Decimal('33'))
        ok, discount, _ = self._validate('THIRTY3')
        self.assertTrue(ok)
        self.assertEqual(discount, 1947)
        self.assertIsInstance(discount, int)

    def test_discount_never_exceeds_the_cart(self):
        """A $500 coupon on a $59 cart is worth $59, not $500 — the total can
        never go negative."""
        make_coupon('HUGE', discount_type='fixed_amount', discount_value=Decimal('500'))
        ok, discount, _ = self._validate('HUGE')
        self.assertTrue(ok)
        self.assertEqual(discount, self.quote.subtotal_cents)
        self.assertEqual(self.quote.subtotal_cents - discount, 0)

    def test_hundred_percent_discount_cannot_go_negative(self):
        make_coupon('FREE', discount_type='percentage', discount_value=Decimal('100'))
        ok, discount, _ = self._validate('FREE')
        self.assertTrue(ok)
        self.assertEqual(discount, self.quote.subtotal_cents)

    # -- invalid ------------------------------------------------------------

    def test_nonexistent_code(self):
        ok, discount, reason = self._validate('NOPE')
        self.assertFalse(ok)
        self.assertEqual(discount, 0)
        self.assertIn('not valid', reason)

    def test_blank_code_is_not_an_error(self):
        ok, discount, reason = self._validate('')
        self.assertFalse(ok)
        self.assertEqual(discount, 0)
        self.assertEqual(reason, '')

    def test_inactive_coupon(self):
        make_coupon('OFF', is_active=False)
        ok, _, reason = self._validate('OFF')
        self.assertFalse(ok)
        self.assertIn('no longer active', reason)

    def test_before_valid_from(self):
        make_coupon('SOON', valid_from=timezone.now() + timedelta(days=2))
        ok, _, reason = self._validate('SOON')
        self.assertFalse(ok)
        self.assertIn('not active yet', reason)

    def test_after_valid_until(self):
        make_coupon('GONE', valid_until=timezone.now() - timedelta(hours=1))
        ok, _, reason = self._validate('GONE')
        self.assertFalse(ok)
        self.assertIn('expired', reason)

    def test_max_uses_reached(self):
        make_coupon('MAXED', max_uses=5, used_count=5)
        ok, _, reason = self._validate('MAXED')
        self.assertFalse(ok)
        self.assertIn('usage limit', reason)

    def test_per_user_limit_reached(self):
        coupon = make_coupon('ONCE', per_user_limit=1)
        payment = Payment.objects.create(user=self.user, order_id='o1', amount='10')
        CouponRedemption.objects.create(coupon=coupon, user=self.user,
                                        payment=payment, discount_applied=Decimal('5'))
        ok, _, reason = self._validate('ONCE')
        self.assertFalse(ok)
        self.assertIn('already used', reason)

    def test_per_user_limit_counts_only_this_user(self):
        coupon = make_coupon('SHARED', per_user_limit=1)
        other = make_user('other@example.com')
        payment = Payment.objects.create(user=other, order_id='o2', amount='10')
        CouponRedemption.objects.create(coupon=coupon, user=other,
                                        payment=payment, discount_applied=Decimal('5'))
        ok, _, reason = self._validate('SHARED')
        self.assertTrue(ok, reason)

    def test_minimum_order_not_met(self):
        make_coupon('BIG', min_order_amount=Decimal('100'))
        ok, _, reason = self._validate('BIG')          # cart is $59
        self.assertFalse(ok)
        self.assertIn('minimum order', reason)

    def test_minimum_order_met(self):
        make_coupon('BIG', min_order_amount=Decimal('50'))
        ok, discount, reason = self._validate('BIG')   # cart is $59
        self.assertTrue(ok, reason)
        self.assertEqual(discount, 1180)

    def test_service_not_applicable(self):
        make_coupon('MKTONLY', applicable_services='email_marketing')
        ok, _, reason = self._validate('MKTONLY')      # cart is validation only
        self.assertFalse(ok)
        self.assertIn('does not apply', reason)

    def test_applicable_services_discounts_only_the_eligible_lines(self):
        """A validation-only coupon on a mixed cart discounts the validation
        portion, not the whole basket."""
        make_coupon('EVONLY', applicable_services='email_validation',
                    discount_type='percentage', discount_value=Decimal('50'))
        mixed = quote_cart({'email_validation': 25_000, 'sales_outreach': 250})
        self.assertEqual(mixed.subtotal_cents, 80900)  # $59 + $750
        ok, discount, reason = self._validate('EVONLY', quote=mixed)
        self.assertTrue(ok, reason)
        self.assertEqual(discount, 2950)               # 50% of the $59 line only

    def test_blank_applicable_services_means_all(self):
        make_coupon('ANY', applicable_services='')
        mixed = quote_cart({'email_validation': 25_000, 'sales_outreach': 250})
        ok, discount, _ = self._validate('ANY', quote=mixed)
        self.assertTrue(ok)
        self.assertEqual(discount, 16180)              # 20% of the full $809

    def test_unknown_service_key_is_ignored_not_fatal(self):
        make_coupon('TYPO', applicable_services='email_validation,not_a_service')
        ok, discount, _ = self._validate('TYPO')
        self.assertTrue(ok)
        self.assertEqual(discount, 1180)

    def test_fixed_credits_is_rejected_rather_than_guessed_at(self):
        make_coupon('CRED', discount_type='fixed_credits', discount_value=Decimal('500'))
        ok, discount, reason = self._validate('CRED')
        self.assertFalse(ok)
        self.assertEqual(discount, 0)
        self.assertIn('cannot be used', reason)

    def test_validation_never_writes_anything(self):
        coupon = make_coupon('READONLY')
        self._validate('READONLY')
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)


# ── checkout with coupons ─────────────────────────────────────────────────────

@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class CouponCheckoutTests(TestCase):

    def setUp(self):
        self.user = make_user('checkout_coupon@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload),
                                content_type='application/json')

    def _order(self, cart, promo='', order_id='order_TEST123'):
        """cart is filled out to include every service (at its minimum) that
        isn't already present -- not required by subscription_order, just a
        convenient rich multi-service cart for these coupon tests."""
        cart = dict(FULL_CART_AT_MINIMUM, **cart)
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay(order_id)) as rz:
            r = self._json('/subscription/order/',
                           {'cart': cart, 'promo_code': promo})
        return r, rz

    def _verify(self, order_id='order_TEST123', signature='sig', raise_sig=False):
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay(order_id, raise_signature=raise_sig)):
            return self._json('/subscription/verify/', {
                'razorpay_order_id':   order_id,
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  signature,
            })

    # -- quote --------------------------------------------------------------

    def test_quote_returns_subtotal_discount_and_total(self):
        make_coupon('SAVE20')
        r = self._json('/subscription/quote/',
                       {'cart': {'email_validation': 25_000}, 'promo_code': 'SAVE20'})
        body = r.json()
        self.assertEqual(body['subtotal_cents'], 5900)
        self.assertEqual(body['discount_cents'], 1180)
        self.assertEqual(body['total_cents'], 4720)
        self.assertEqual(body['promo_code'], 'SAVE20')
        self.assertTrue(body['promo_applied'])

    def test_quote_with_invalid_code_prices_the_cart_anyway(self):
        r = self._json('/subscription/quote/',
                       {'cart': {'email_validation': 25_000}, 'promo_code': 'NOPE'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['discount_cents'], 0)
        self.assertEqual(body['total_cents'], 5900)
        self.assertFalse(body['promo_applied'])
        self.assertIn('not valid', body['promo_message'])

    def test_quote_does_not_increment_used_count(self):
        coupon = make_coupon('SAVE20')
        for _ in range(5):
            self._json('/subscription/quote/',
                       {'cart': {'email_validation': 25_000}, 'promo_code': 'SAVE20'})
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)

    # -- order --------------------------------------------------------------

    def test_order_charges_the_discounted_amount(self):
        # Full 7-service cart (email_validation overridden to 25,000, the
        # other 6 filled in at their minimum by _order()): subtotal is
        # 7040 cents ($70.40), 20% off is 1408 cents ($14.08).
        make_coupon('SAVE20')
        r, rz = self._order({'email_validation': 25_000}, promo='SAVE20')
        self.assertEqual(r.status_code, 200, r.content)

        sent = rz.return_value.order.create.call_args.kwargs['data']
        self.assertEqual(sent['amount'], 5632)
        self.assertIsInstance(sent['amount'], int)

        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertEqual(order.subtotal_cents, 7040)
        self.assertEqual(order.discount_cents, 1408)
        self.assertEqual(order.amount_cents, 5632)
        self.assertEqual(order.promo_code, 'SAVE20')
        self.assertEqual(order.coupon.code, 'SAVE20')

        # The "Confirm your purchase" / "Order Summary" popup shows this
        # verbatim as the discount row -- a dollar-off label, since a
        # coupon's discount isn't generally a clean percentage.
        self.assertEqual(r.json()['discount_label'], '−$14.08 off')

    def test_order_does_not_increment_used_count(self):
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)

    def test_order_refuses_an_invalid_code_rather_than_ignoring_it(self):
        r, rz = self._order({'email_validation': 25_000}, promo='NOPE')
        self.assertEqual(r.status_code, 400)
        self.assertIn('not valid', r.json()['message'])
        rz.return_value.order.create.assert_not_called()
        self.assertFalse(ServiceOrder.objects.exists())

    def test_coupon_cannot_bypass_the_minimum_credit_quantity(self):
        """A 100%-off coupon still cannot make a below-minimum quantity
        purchasable -- the floor is on the quantity, checked in quote_cart()
        before any discount is even looked at, so no discount can waive it.
        Email Validation's minimum is 1,000; 249 is well below it."""
        make_coupon('FREE100', discount_type='percentage', discount_value=Decimal('100'))
        r, rz = self._order({'email_validation': 249}, promo='FREE100')
        self.assertEqual(r.status_code, 400)
        self.assertIn('1,000', r.json()['message'])
        rz.return_value.order.create.assert_not_called()
        self.assertFalse(ServiceOrder.objects.exists())

    def test_order_ignores_a_forged_discount(self):
        """The browser sends quantities and a code. Everything else is noise."""
        make_coupon('SAVE20')
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()) as rz:
            self._json('/subscription/order/', {
                'cart': dict(FULL_CART_AT_MINIMUM, email_validation=25_000),
                'promo_code': 'SAVE20',
                # all lies
                'discount_cents': 5800, 'discount': 58.00,
                'total_cents': 1, 'amount_cents': 1, 'subtotal_cents': 1,
                'price': 1, 'amount': 1,
            })
        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertEqual(order.discount_cents, 1408)    # the real 20%
        self.assertEqual(order.amount_cents, 5632)
        self.assertEqual(order.subtotal_cents, 7040)
        self.assertEqual(rz.return_value.order.create.call_args.kwargs['data']['amount'], 5632)

    def test_order_cannot_be_given_a_coupon_it_did_not_validate(self):
        """Passing a coupon id/object directly must have no effect — only the
        code is read, and only through validate_coupon."""
        other = make_coupon('SECRET', discount_type='percentage',
                            discount_value=Decimal('90'), is_active=False)
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self._json('/subscription/order/', {
                'cart': dict(FULL_CART_AT_MINIMUM, email_validation=25_000),
                'coupon': other.pk, 'coupon_id': other.pk,
                'discount_cents': 5310,
            })
        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertIsNone(order.coupon)
        self.assertEqual(order.discount_cents, 0)
        self.assertEqual(order.amount_cents, 7040)

    # -- verify -------------------------------------------------------------

    def test_successful_verification_redeems_exactly_once(self):
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        r = self._verify()
        self.assertEqual(r.status_code, 200, r.content)

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

        redemption = CouponRedemption.objects.get()
        self.assertEqual(redemption.coupon_id, coupon.pk)
        self.assertEqual(redemption.user_id, self.user.id)
        self.assertEqual(redemption.discount_applied, Decimal('14.08'))
        self.assertEqual(redemption.payment.order_id, 'order_TEST123')

        # And the purchase itself completed normally.
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)
        self.assertEqual(ServiceOrder.objects.get().status, ServiceOrder.STATUS_PAID)

    def test_replayed_verification_does_not_redeem_again(self):
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        self._verify()
        self._verify()
        self._verify()

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(CouponRedemption.objects.count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)

    def test_failed_signature_does_not_redeem(self):
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        r = self._verify(raise_sig=True, signature='forged')

        self.assertEqual(r.status_code, 400)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    def test_abandoned_order_never_redeems(self):
        """An order created but never paid must leave the coupon untouched."""
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)

    def test_verify_ignores_a_forged_discount_in_the_payload(self):
        coupon = make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'sig',
                'discount_cents': 5900, 'amount': 0, 'total_cents': 0,
                'cart': {'email_validation': 9_999_999},
                'promo_code': 'SOMETHINGELSE',
            })

        redemption = CouponRedemption.objects.get()
        self.assertEqual(redemption.discount_applied, Decimal('14.08'))
        self.assertEqual(redemption.coupon_id, coupon.pk)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)

    def test_purchase_without_a_coupon_creates_no_redemption(self):
        self._order({'email_validation': 25_000})
        self._verify()
        self.assertEqual(CouponRedemption.objects.count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)

    def test_redemption_is_recorded_against_the_payment_row(self):
        make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        self._verify()

        payment = Payment.objects.get(order_id='order_TEST123')
        self.assertEqual(payment.promo_code, 'SAVE20')
        self.assertEqual(payment.discount_amount, Decimal('14.08'))
        self.assertEqual(payment.coupon_redemption.discount_applied, Decimal('14.08'))

    def test_per_user_limit_blocks_the_second_order(self):
        make_coupon('ONCE', per_user_limit=1)
        self._order({'email_validation': 25_000}, promo='ONCE')
        self._verify()

        r, rz = self._order({'email_validation': 25_000}, promo='ONCE',
                            order_id='order_SECOND')
        self.assertEqual(r.status_code, 400)
        self.assertIn('already used', r.json()['message'])
        self.assertEqual(CouponRedemption.objects.count(), 1)

    def test_max_uses_blocks_the_next_order(self):
        coupon = make_coupon('ONLYONE', max_uses=1)
        self._order({'email_validation': 25_000}, promo='ONLYONE')
        self._verify()
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

        r, _ = self._order({'email_validation': 25_000}, promo='ONLYONE',
                           order_id='order_SECOND')
        self.assertEqual(r.status_code, 400)
        self.assertIn('usage limit', r.json()['message'])

    def test_legacy_balances_untouched_by_a_coupon_purchase(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50,
                                      cc_current_credits=100, vc_current_credits=7000)
        make_coupon('SAVE20')
        self._order({'email_validation': 25_000}, promo='SAVE20')
        self._verify()

        cc = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual((cc.ac_current_credits, cc.cc_current_credits,
                          cc.vc_current_credits), (50, 100, 7000))


# ── concurrency ───────────────────────────────────────────────────────────────

@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class CouponConcurrencyTests(TransactionTestCase):
    """TransactionTestCase, not TestCase: real threads need real committed
    transactions, which the usual wrapping rollback would hide.

    serialized_rollback = True: without it, this test's teardown flushes
    every Django-managed table (including migration-seeded data, e.g.
    CreditPackage's pricing rows from 0135_fix_credit_package_pricing) and
    never restores it -- a data migration's RunPython does not re-run on an
    already-migrated database. This flag has Django re-seed that data from a
    snapshot taken when the ISOLATED TEST database (never the real one --
    this class always runs via `manage.py test`, never `manage.py shell`)
    was built, which protects any test that runs afterward IN THE SAME
    `manage.py test` invocation (the common case, and the only one this
    project's default test ordering relies on).

    Known remaining gap, confirmed by testing it directly: `--keepdb` does
    NOT re-take that snapshot when it reuses an already-existing test
    database, so a `--keepdb` run immediately after one where this class's
    flush already fired will NOT have its data restored, and subsequent
    pricing-dependent tests will fail with "No pricing is configured" until
    the test database is rebuilt fresh. Verification / CI runs should build
    the test database fresh (no `--keepdb`) rather than rely on this flag to
    make repeated `--keepdb` reuse safe.
    """
    serialized_rollback = True

    def test_two_simultaneous_redemptions_cannot_overrun_max_uses(self):
        import threading
        from django.db import connections

        coupon = make_coupon('RACE', max_uses=1)
        users, orders = [], []
        for i in range(2):
            user = make_user(f'race{i}@example.com')
            users.append(user)
            payment = Payment.objects.create(user=user, order_id=f'order_R{i}',
                                             amount='47.20')
            order = ServiceOrder.objects.create(
                user=user, order_id=f'order_R{i}',
                cart_json={'email_validation': 25_000},
                subtotal_cents=5900, discount_cents=1180, amount_cents=4720,
                currency='USD', promo_code='RACE', coupon=coupon)
            orders.append((order, payment, user))

        barrier = threading.Barrier(2)
        results = []

        def redeem(order, payment, user):
            from django.db import transaction
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    locked = ServiceOrder.objects.select_for_update().get(pk=order.pk)
                    r = coupon_service.redeem_coupon(locked, payment, user_id=user.id)
                    results.append(r is not None)
            except Exception as e:
                results.append(e)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=redeem, args=args) for args in orders]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1,
                         f"max_uses=1 was overrun; results={results}")
        self.assertEqual(CouponRedemption.objects.filter(coupon=coupon).count(), 1)
        self.assertEqual(sorted(r for r in results if isinstance(r, bool)),
                         [False, True])

    def test_two_simultaneous_redemptions_cannot_overrun_per_user_limit(self):
        import threading
        from django.db import connections, transaction

        coupon = make_coupon('PERUSER', per_user_limit=1)
        user = make_user('peruser@example.com')
        jobs = []
        for i in range(2):
            payment = Payment.objects.create(user=user, order_id=f'order_P{i}',
                                             amount='47.20')
            order = ServiceOrder.objects.create(
                user=user, order_id=f'order_P{i}',
                cart_json={'email_validation': 25_000},
                subtotal_cents=5900, discount_cents=1180, amount_cents=4720,
                currency='USD', promo_code='PERUSER', coupon=coupon)
            jobs.append((order, payment))

        barrier = threading.Barrier(2)
        results = []

        def redeem(order, payment):
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    locked = ServiceOrder.objects.select_for_update().get(pk=order.pk)
                    r = coupon_service.redeem_coupon(locked, payment, user_id=user.id)
                    results.append(r is not None)
            except Exception as e:
                results.append(e)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=redeem, args=j) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(CouponRedemption.objects.filter(coupon=coupon, user=user).count(), 1,
                         f"per_user_limit=1 was overrun; results={results}")
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
