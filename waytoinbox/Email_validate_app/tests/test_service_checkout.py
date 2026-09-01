"""Tests for Phase 3: the service-credit checkout endpoints.

The point of these endpoints is that the browser is not trusted with money.
The tests that matter most are therefore the adversarial ones: tampering with
the verify payload, replaying it, and reaching for someone else's order.

Razorpay is mocked throughout — no network call is made and no real payment is
created. The signature check is exercised in both directions by making the
mocked utility raise or not raise.
"""
import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client, override_settings
from razorpay.errors import SignatureVerificationError

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, ServiceOrder, Payment,
    CreditAuditLog,
)
from Email_validate_app.services.credit_manager import get_service_balance


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Checkout Test', user_email=email, password='StrongPass123!')


def fake_razorpay(order_id='order_TEST123', raise_signature=False):
    """A Razorpay client stand-in. order.create echoes a fixed id; the
    signature utility either passes silently or raises."""
    client = MagicMock()
    client.order.create.return_value = {'id': order_id, 'amount': 0, 'currency': 'USD'}
    if raise_signature:
        client.utility.verify_payment_signature.side_effect = SignatureVerificationError('bad')
    else:
        client.utility.verify_payment_signature.return_value = None
    client.payment.fetch.return_value = {
        'email': 'payer@example.com', 'contact': '+10000000000', 'method': 'card',
    }
    return client


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class ServiceCheckoutTests(TestCase):

    def setUp(self):
        self.user = make_user('checkout@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload),
                                content_type='application/json')

    # -- quote ---------------------------------------------------------------

    def test_quote_prices_the_cart_server_side(self):
        r = self._json('/subscription/quote/', {'cart': {
            'email_validation': 25_000, 'email_marketing': 5_000, 'sales_outreach': 10}})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['subtotal_cents'], 9900)     # $59 + $10 + $30
        self.assertEqual(body['total_cents'], 9900)
        self.assertEqual(len(body['lines']), 3)

    def test_quote_never_exposes_a_per_credit_rate(self):
        r = self._json('/subscription/quote/', {'cart': {'email_validation': 25_000}})
        self.assertNotIn('per_credit', r.content.decode().lower())
        self.assertNotIn('0.00', r.json()['lines'][0]['price'])

    def test_quote_rejects_garbage(self):
        for cart in ({'email_validation': -5}, {'email_validation': 'abc'},
                     {'not_a_service': 10}, {'email_validation': 1.5}):
            r = self._json('/subscription/quote/', {'cart': cart})
            self.assertEqual(r.status_code, 400, f"accepted {cart!r}")

    def test_quote_requires_login(self):
        r = Client(SERVER_NAME='127.0.0.1').post(
            '/subscription/quote/', data=json.dumps({'cart': {}}),
            content_type='application/json')
        self.assertEqual(r.status_code, 401)

    # -- order ---------------------------------------------------------------

    def test_order_freezes_the_server_quote(self):
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()) as rz:
            r = self._json('/subscription/order/', {'cart': {
                'email_validation': 25_000, 'sales_outreach': 10}})

        self.assertEqual(r.status_code, 200, r.content)
        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertEqual(order.amount_cents, 8900)                 # $59 + $30
        self.assertEqual(order.cart_json, {'email_validation': 25_000, 'sales_outreach': 10})
        self.assertEqual(order.status, ServiceOrder.STATUS_CREATED)
        self.assertEqual(order.currency, 'USD')

        # Razorpay was asked for an integer amount, in the server's currency.
        sent = rz.return_value.order.create.call_args.kwargs['data']
        self.assertEqual(sent['amount'], 8900)
        self.assertIsInstance(sent['amount'], int)
        self.assertEqual(sent['currency'], 'USD')

    def test_order_ignores_any_price_the_browser_sends(self):
        """A price/amount/total in the payload must have no effect."""
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            self._json('/subscription/order/', {
                'cart': {'email_validation': 25_000},
                'price': 1, 'amount': 1, 'amount_cents': 1, 'total_cents': 1,
                'currency': 'INR', 'discount_cents': 5900,
            })
        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertEqual(order.amount_cents, 5900)   # the real $59
        self.assertEqual(order.currency, 'USD')
        self.assertEqual(order.discount_cents, 0)

    def test_order_rejects_an_empty_cart(self):
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self._json('/subscription/order/', {'cart': {'email_validation': 0}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ServiceOrder.objects.exists())

    def test_order_creates_nothing_when_the_cart_is_invalid(self):
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()) as rz:
            r = self._json('/subscription/order/', {'cart': {'email_validation': -1}})
        self.assertEqual(r.status_code, 400)
        rz.return_value.order.create.assert_not_called()
        self.assertFalse(ServiceOrder.objects.exists())

    # -- verify --------------------------------------------------------------

    def _make_order(self, cart, order_id='order_TEST123'):
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay(order_id)):
            self._json('/subscription/order/', {'cart': cart})
        return ServiceOrder.objects.get(order_id=order_id)

    def test_verify_credits_exactly_what_the_order_row_says(self):
        self._make_order({'email_validation': 25_000, 'sales_outreach': 10})

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'sig',
            })

        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 10)
        self.assertEqual(
            ServiceOrder.objects.get(order_id='order_TEST123').status,
            ServiceOrder.STATUS_PAID)
        self.assertTrue(Payment.objects.filter(order_id='order_TEST123').exists())

    def test_verify_ignores_a_forged_cart_in_the_payload(self):
        """THE tamper test. The POST claims a million credits; the order row
        says 25,000. The order row wins."""
        self._make_order({'email_validation': 25_000})

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'sig',
                # everything below is a lie
                'cart':     {'email_validation': 1_000_000, 'sales_outreach': 500},
                'credits':  1_000_000,
                'amount':   1,
                'quantity': 1_000_000,
            })

        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)

    def test_verify_is_exactly_once_when_replayed(self):
        self._make_order({'email_validation': 25_000})
        payload = {
            'razorpay_order_id':   'order_TEST123',
            'razorpay_payment_id': 'pay_TEST123',
            'razorpay_signature':  'sig',
        }
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            first  = self._json('/subscription/verify/', payload)
            second = self._json('/subscription/verify/', payload)
            third  = self._json('/subscription/verify/', payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json().get('already_processed'))
        self.assertTrue(third.json().get('already_processed'))
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 25_000)
        self.assertEqual(Payment.objects.filter(order_id='order_TEST123').count(), 1)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_id='order_TEST123').count(), 1)

    def test_verify_credits_nothing_when_the_signature_fails(self):
        self._make_order({'email_validation': 25_000})

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay(raise_signature=True)):
            r = self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'forged',
            })

        self.assertEqual(r.status_code, 400)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        self.assertFalse(Payment.objects.filter(order_id='order_TEST123').exists())
        self.assertEqual(
            ServiceOrder.objects.get(order_id='order_TEST123').status,
            ServiceOrder.STATUS_FAILED)

    def test_verify_cannot_claim_another_users_order(self):
        self._make_order({'email_validation': 25_000})

        attacker = make_user('attacker@example.com')
        c = Client(SERVER_NAME='127.0.0.1')
        s = c.session
        s['logged_in'] = attacker.user_email
        s.save()

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = c.post('/subscription/verify/',
                       data=json.dumps({'razorpay_order_id':   'order_TEST123',
                                        'razorpay_payment_id': 'pay_TEST123',
                                        'razorpay_signature':  'sig'}),
                       content_type='application/json')

        self.assertEqual(r.status_code, 404)
        self.assertEqual(get_service_balance(attacker.id, 'email_validation'), 0)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    def test_verify_rejects_a_missing_signature(self):
        self._make_order({'email_validation': 25_000})
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
            })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    def test_verify_leaves_legacy_wallets_untouched(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50,
                                      cc_current_credits=100, vc_current_credits=7000)
        self._make_order({'email_validation': 25_000, 'reputation': 50})

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            self._json('/subscription/verify/', {
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'sig',
            })

        cc = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual((cc.ac_current_credits, cc.cc_current_credits,
                          cc.vc_current_credits), (50, 100, 7000))
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 50)

    def test_get_is_not_allowed_on_any_endpoint(self):
        for url in ('/subscription/quote/', '/subscription/order/', '/subscription/verify/'):
            self.assertEqual(self.client.get(url).status_code, 405, url)
