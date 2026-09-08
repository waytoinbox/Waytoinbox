"""Legacy PAYG/subscription payment ownership must survive a Main<->Sub
Account context switch between order creation and verification.

views/billing.py::payment() and views/subscription.py::subs_payment() both
resolved the crediting account fresh, at verify time, with no link back to
whoever actually created the Razorpay order -- so switching the active
account in between (a one-click action once Sub Accounts exist) could move
a payment/credits to a different account than the one that started it.
Confirmed exploitable during independent verification of the Sub Account
feature (see that report's Finding 1). Fixed by recording the creating
account in cache at order-creation time (billing.py::_remember_legacy_order_owner)
and requiring an exact match at verify time -- mirrors the invariant
views/credits.py already gets for free from its ServiceOrder row.

views/credits.py::subscription_verify() was already safe and is not
touched here; test_service_checkout.py's own suite is the regression guard
for it and is re-run, not modified, alongside these.
"""
import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, Client, override_settings

from Email_validate_app.models import UserTable, Payment, SubsPayment, CurrentCredits


def make_user(email, parent=None):
    user = UserTable.objects.create_user(
        user_name='Legacy Payment Test', user_email=email, password='StrongPass123!')
    user.is_verified = True
    if parent is not None:
        user.parent_account = parent
    user.save()
    return user


def fake_razorpay(order_id='order_LEGACY_TEST'):
    client = MagicMock()
    client.order.create.return_value = {'id': order_id, 'amount': 1000, 'currency': 'USD'}
    client.utility.verify_payment_signature.return_value = None
    client.payment.fetch.return_value = {'email': None, 'contact': None, 'amount': 1000}
    return client


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class PaygPaymentOwnershipAcrossSwitchTests(TestCase):
    """views/billing.py::order_payment() / payment()"""

    def setUp(self):
        cache.clear()
        self.main = make_user('legacy-main@company.com')
        self.sub1 = make_user('legacy-sub1@company.com', parent=self.main)
        self.sub2 = make_user('legacy-sub2@company.com', parent=self.main)
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.main.user_email
        session.save()

    def _switch(self, email):
        return self.client.post('/account/switch/', data=json.dumps({'email': email}),
                                 content_type='application/json')

    def _return_to_main(self):
        return self.client.post('/account/return/')

    def _create_order(self, order_id='order_LEGACY_TEST'):
        with patch('razorpay.Client', return_value=fake_razorpay(order_id)):
            return self.client.post('/pricing/order_payment/', {
                'plan': '1000', 'price': '10', 'pricePerEmail': '0.01', 'usd-inr': 'USD',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def _verify(self, order_id='order_LEGACY_TEST', payment_id='pay_LEGACY_TEST'):
        with patch('razorpay.Client', return_value=fake_razorpay(order_id)):
            return self.client.post('/pricing/order_payment/payment/', {
                'payment_id': payment_id, 'order_id': order_id,
                'razorpay_signature': 'sig', 'credits': '1000', 'currency': 'USD',
                'plan': '10', 'description': 'test',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_main_creates_and_main_verifies(self):
        self._create_order()
        r = self._verify()
        self.assertEqual(r.json().get('status'), 'ok', r.content)
        payment = Payment.objects.get(order_id='order_LEGACY_TEST')
        self.assertEqual(payment.user_id, self.main.id)

    def test_sub1_creates_and_sub1_verifies(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        r = self._verify()
        self.assertEqual(r.json().get('status'), 'ok', r.content)
        payment = Payment.objects.get(order_id='order_LEGACY_TEST')
        self.assertEqual(payment.user_id, self.sub1.id)

    def test_sub1_creates_main_verifies_is_rejected(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        self._return_to_main()

        r = self._verify()
        body = r.json()

        self.assertEqual(body.get('status'), 'error')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Payment.objects.filter(order_id='order_LEGACY_TEST').exists())
        main_cc = CurrentCredits.objects.filter(user_id=self.main.id).first()
        self.assertTrue(main_cc is None or main_cc.vc_current_credits == 0)

    def test_sub1_creates_sub2_verifies_is_rejected(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        self._switch(self.sub2.user_email)

        r = self._verify()
        body = r.json()

        self.assertEqual(body.get('status'), 'error')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Payment.objects.filter(order_id='order_LEGACY_TEST').exists())
        sub2_cc = CurrentCredits.objects.filter(user_id=self.sub2.id).first()
        self.assertTrue(sub2_cc is None or sub2_cc.vc_current_credits == 0)

    def test_legitimate_retry_after_success_still_reports_already_processed(self):
        """A double-click by the SAME account that created the order must
        still hit the existing idempotency guard, not the new ownership
        check -- ownership lookup peeks the cache rather than consuming it."""
        self._create_order()
        self._verify()
        r = self._verify()
        self.assertEqual(r.json().get('status'), 'ok')
        self.assertEqual(Payment.objects.filter(order_id='order_LEGACY_TEST').count(), 1)


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class SubscriptionPaymentOwnershipAcrossSwitchTests(TestCase):
    """views/subscription.py::create_subscription() / subs_payment()"""

    def setUp(self):
        cache.clear()
        self.main = make_user('legacy-subs-main@company.com')
        self.sub1 = make_user('legacy-subs-sub1@company.com', parent=self.main)
        self.sub2 = make_user('legacy-subs-sub2@company.com', parent=self.main)
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.main.user_email
        session.save()

    def _switch(self, email):
        return self.client.post('/account/switch/', data=json.dumps({'email': email}),
                                 content_type='application/json')

    def _return_to_main(self):
        return self.client.post('/account/return/')

    def _create_order(self, order_id='order_SUBS_TEST'):
        with patch('razorpay.Client', return_value=fake_razorpay(order_id)):
            return self.client.post('/create_subscription/', {
                'plan': 'Classic', 'price': '10', 'billing_cycle': 'monthly', 'contacts': '0',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def _verify(self, order_id='order_SUBS_TEST', payment_id='pay_SUBS_TEST'):
        with patch('razorpay.Client', return_value=fake_razorpay(order_id)):
            return self.client.post('/subs_payment/', {
                'payment_id': payment_id, 'order_id': order_id,
                'razorpay_signature': 'sig', 'credits': 'Classic', 'currency': 'USD',
                'description': 'test', 'contacts': '0',
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_main_creates_and_main_verifies(self):
        r0 = self._create_order()
        self.assertEqual(r0.json().get('status'), 'ok', r0.content)
        r = self._verify()
        self.assertEqual(r.json().get('status'), 'ok', r.content)
        payment = SubsPayment.objects.get(order_id='order_SUBS_TEST')
        self.assertEqual(payment.user_id, self.main.id)

    def test_sub1_creates_and_sub1_verifies(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        r = self._verify()
        self.assertEqual(r.json().get('status'), 'ok', r.content)
        payment = SubsPayment.objects.get(order_id='order_SUBS_TEST')
        self.assertEqual(payment.user_id, self.sub1.id)

    def test_sub1_creates_main_verifies_is_rejected(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        self._return_to_main()

        r = self._verify()
        self.assertEqual(r.json().get('status'), 'error')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(SubsPayment.objects.filter(order_id='order_SUBS_TEST').exists())

    def test_sub1_creates_sub2_verifies_is_rejected(self):
        self._switch(self.sub1.user_email)
        self._create_order()
        self._switch(self.sub2.user_email)

        r = self._verify()
        self.assertEqual(r.json().get('status'), 'error')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(SubsPayment.objects.filter(order_id='order_SUBS_TEST').exists())
