"""Main Account -> Sub Account hierarchy.

Sub Accounts are plain UserTable rows (UserTable.parent_account, see
migration 0141_main_sub_account_hierarchy) -- isolation, credit wallets and
payment ownership all fall out for free from the app's existing
user_id-scoped queries once utils.get_active_user()/get_user_id() resolve
the correct account (see utils.py and context_processors.py::nav_credits).
These tests verify that resolution plus the new create/switch/return/
overview endpoints; they deliberately do not re-test credit_manager.py's or
the Razorpay checkout flow's own internals (already covered elsewhere) --
only that those existing systems key off the correct account_id here.
"""
import json

from unittest.mock import patch

from django.test import TestCase, Client, RequestFactory, override_settings

from Email_validate_app.models import UserTable, ServiceOrder, Payment
from Email_validate_app.services.credit_manager import (
    add_service_credits, deduct_service_credits, get_service_balance,
)
from Email_validate_app.utils import get_user_id, get_active_user, get_true_user
from Email_validate_app.tests.test_service_checkout import fake_razorpay, FULL_CART_AT_MINIMUM


def make_user(email, verified=True, parent=None):
    user = UserTable.objects.create_user(
        user_name='Sub Acct Test', user_email=email, password='StrongPass123!')
    user.is_verified = verified
    if parent is not None:
        user.parent_account = parent
    user.save()
    return user


def _client_for(email):
    c = Client(SERVER_NAME='127.0.0.1')
    session = c.session
    session['logged_in'] = email
    session.save()
    return c


def _resolve_id(client):
    """The account get_user_id() resolves for this test client's current
    session -- exercises the exact same code path a real view would."""
    request = RequestFactory().get('/')
    request.session = client.session
    return get_user_id(request)


_BASE_SETTINGS = dict(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)


@override_settings(**_BASE_SETTINGS)
class SubAccountCreationTests(TestCase):
    def setUp(self):
        self.main = make_user('main@company.com')
        self.client = _client_for(self.main.user_email)

    def _create(self, email, name='Sub One', password='StrongPass123!'):
        return self.client.post('/sub-accounts/create/', {
            'user_name': name, 'user_email': email,
            'password': password, 'confirm_password': password,
        })

    def test_main_can_create_sub_account(self):
        body = self._create('sub1@company.com').json()
        self.assertEqual(body['status'], 'ok')
        sub = UserTable.objects.get(user_email='sub1@company.com')
        self.assertEqual(sub.parent_account_id, self.main.id)
        self.assertFalse(sub.is_verified)
        self.assertTrue(sub.check_password('StrongPass123!'))

    def test_main_can_create_multiple_sub_accounts(self):
        self._create('sub1@company.com')
        self._create('sub2@company.com')
        self.assertEqual(
            UserTable.objects.filter(parent_account_id=self.main.id).count(), 2)

    def test_sub_account_cannot_create_another_sub_account(self):
        sub = make_user('subx@company.com', parent=self.main)
        sub_client = _client_for(sub.user_email)
        r = sub_client.post('/sub-accounts/create/', {
            'user_name': 'Nested', 'user_email': 'subx1@company.com',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!',
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse(UserTable.objects.filter(user_email='subx1@company.com').exists())

    def test_free_email_domain_rejected_for_sub_account(self):
        body = self._create('sub1@gmail.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertFalse(UserTable.objects.filter(user_email='sub1@gmail.com').exists())

    def test_duplicate_email_rejected(self):
        self._create('sub1@company.com')
        body = self._create('sub1@company.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertEqual(UserTable.objects.filter(user_email='sub1@company.com').count(), 1)

    def test_existing_user_created_before_this_feature_is_a_main_account(self):
        legacy = make_user('legacy@example.com')
        self.assertIsNone(legacy.parent_account_id)


@override_settings(**_BASE_SETTINGS)
class SubAccountDomainMatchTests(TestCase):
    """A Sub Account must share its Main Account's email domain -- a
    business rule only; ownership/isolation still comes solely from
    parent_account (verified separately below), never from the domain."""

    def setUp(self):
        self.main = make_user('main@abc.com')
        self.client = _client_for(self.main.user_email)

    def _create(self, email, name='Sub', password='StrongPass123!'):
        return self.client.post('/sub-accounts/create/', {
            'user_name': name, 'user_email': email,
            'password': password, 'confirm_password': password,
        })

    def test_same_domain_allowed(self):
        body = self._create('sub1@abc.com').json()
        self.assertEqual(body['status'], 'ok')
        sub = UserTable.objects.get(user_email='sub1@abc.com')
        self.assertEqual(sub.parent_account_id, self.main.id)

    def test_different_business_domain_rejected(self):
        body = self._create('sub1@xyz.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('same email domain', body['message'].lower())
        self.assertFalse(UserTable.objects.filter(user_email='sub1@xyz.com').exists())

    def test_free_public_domain_still_rejected_by_the_existing_rule(self):
        # Must remain blocked by is_free_email_domain(), not the new
        # domain-match check -- gmail.com never matches any Main's domain
        # anyway, but the *reason* given should still be the free-domain
        # policy, since that check runs first.
        body = self._create('sub1@gmail.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('business email', body['message'].lower())
        self.assertFalse(UserTable.objects.filter(user_email='sub1@gmail.com').exists())

    def test_case_insensitive_main_uppercase(self):
        # UserManager.create_user() would normalize the domain to lowercase
        # on its own (Django's normalize_email) -- bypass that here so this
        # test actually exercises the view's own case handling, matching
        # real signup (CustomSignupForm.save(), no such normalization).
        main_upper = make_user('mainupper@abc.com')
        UserTable.objects.filter(pk=main_upper.pk).update(user_email='mainupper@ABC.COM')
        main_upper.refresh_from_db()
        self.assertEqual(main_upper.user_email, 'mainupper@ABC.COM')

        client = _client_for(main_upper.user_email)
        r = client.post('/sub-accounts/create/', {
            'user_name': 'Sub', 'user_email': 'subupper@abc.com',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!',
        })
        self.assertEqual(r.json()['status'], 'ok', r.content)

    def test_case_insensitive_sub_uppercase(self):
        r = self._create('SUB2@ABC.COM')
        self.assertEqual(r.json()['status'], 'ok', r.content)

    def test_different_main_accounts_sharing_a_domain_are_still_isolated(self):
        main_a = self.main  # main@abc.com
        main_b = make_user('main2@abc.com')

        body_a = self._create('suba@abc.com').json()
        self.assertEqual(body_a['status'], 'ok')

        client_b = _client_for(main_b.user_email)
        r_b = client_b.post('/sub-accounts/create/', {
            'user_name': 'Sub B', 'user_email': 'subb@abc.com',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!',
        })
        self.assertEqual(r_b.json()['status'], 'ok')

        sub_a = UserTable.objects.get(user_email='suba@abc.com')
        sub_b = UserTable.objects.get(user_email='subb@abc.com')
        self.assertEqual(sub_a.parent_account_id, main_a.id)
        self.assertEqual(sub_b.parent_account_id, main_b.id)

        # Domain sharing must not leak ownership either way.
        overview_a = self.client.get('/sub-accounts/').content.decode()
        self.assertIn('suba@abc.com', overview_a)
        self.assertNotIn('subb@abc.com', overview_a)

        overview_b = client_b.get('/sub-accounts/').content.decode()
        self.assertIn('subb@abc.com', overview_b)
        self.assertNotIn('suba@abc.com', overview_b)

        r_switch = client_b.post('/account/switch/', data=json.dumps({'email': 'suba@abc.com'}),
                                  content_type='application/json')
        self.assertNotEqual(r_switch.json().get('status'), 'ok')

    def test_sub_account_creating_another_sub_account_still_rejected(self):
        sub = make_user('subnest@abc.com', parent=self.main)
        sub_client = _client_for(sub.user_email)
        r = sub_client.post('/sub-accounts/create/', {
            'user_name': 'Nested', 'user_email': 'subnest2@abc.com',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!',
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse(UserTable.objects.filter(user_email='subnest2@abc.com').exists())


@override_settings(**_BASE_SETTINGS)
class LoginTests(TestCase):
    def setUp(self):
        self.main = make_user('main2@company.com')
        self.sub = make_user('sub2@company.com', parent=self.main)

    def test_main_login_works(self):
        c = Client(SERVER_NAME='127.0.0.1')
        r = c.post('/login/', {'email': self.main.user_email, 'password': 'StrongPass123!'})
        self.assertEqual(r.json()['status'], 'ok')

    def test_sub_account_login_works(self):
        c = Client(SERVER_NAME='127.0.0.1')
        r = c.post('/login/', {'email': self.sub.user_email, 'password': 'StrongPass123!'})
        self.assertEqual(r.json()['status'], 'ok')

    def test_direct_sub_account_login_has_no_acting_context(self):
        c = Client(SERVER_NAME='127.0.0.1')
        c.post('/login/', {'email': self.sub.user_email, 'password': 'StrongPass123!'})
        self.assertNotIn('acting_as_email', c.session)
        self.assertEqual(_resolve_id(c), self.sub.id)


@override_settings(**_BASE_SETTINGS)
class SwitchAccountTests(TestCase):
    def setUp(self):
        self.main_a = make_user('maina@company.com')
        self.sub1 = make_user('sub1@companya.com', parent=self.main_a)
        self.sub2 = make_user('sub2@companya.com', parent=self.main_a)
        self.main_b = make_user('mainb@company.com')
        self.sub_b1 = make_user('subb1@companyb.com', parent=self.main_b)

    def _switch(self, client, email):
        return client.post('/account/switch/', data=json.dumps({'email': email}),
                            content_type='application/json')

    def test_main_switch_to_own_sub_succeeds(self):
        c = _client_for(self.main_a.user_email)
        body = self._switch(c, self.sub1.user_email).json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(c.session['acting_as_email'], self.sub1.user_email)
        self.assertEqual(_resolve_id(c), self.sub1.id)

    def test_main_switch_to_own_second_sub_succeeds(self):
        c = _client_for(self.main_a.user_email)
        body = self._switch(c, self.sub2.user_email).json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(_resolve_id(c), self.sub2.id)

    def test_main_cannot_switch_to_another_main(self):
        c = _client_for(self.main_a.user_email)
        r = self._switch(c, self.main_b.user_email)
        self.assertNotEqual(r.json().get('status'), 'ok')
        self.assertNotIn('acting_as_email', c.session)
        self.assertEqual(_resolve_id(c), self.main_a.id)

    def test_main_cannot_switch_to_another_mains_sub(self):
        c = _client_for(self.main_a.user_email)
        r = self._switch(c, self.sub_b1.user_email)
        self.assertNotEqual(r.json().get('status'), 'ok')
        self.assertEqual(_resolve_id(c), self.main_a.id)

    def test_sub_cannot_switch_to_sibling(self):
        c = _client_for(self.sub1.user_email)
        r = self._switch(c, self.sub2.user_email)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(_resolve_id(c), self.sub1.id)

    def test_sub_cannot_switch_to_parent(self):
        c = _client_for(self.sub1.user_email)
        r = self._switch(c, self.main_a.user_email)
        self.assertEqual(r.status_code, 403)

    def test_sub_cannot_switch_to_unrelated_account(self):
        c = _client_for(self.sub1.user_email)
        r = self._switch(c, self.main_b.user_email)
        self.assertEqual(r.status_code, 403)

    def test_switch_to_nonexistent_email_fails(self):
        c = _client_for(self.main_a.user_email)
        r = self._switch(c, 'nobody@companya.com')
        self.assertNotEqual(r.json().get('status'), 'ok')


@override_settings(**_BASE_SETTINGS)
class ReturnToMainTests(TestCase):
    def setUp(self):
        self.main = make_user('mainr@company.com')
        self.sub = make_user('subr@company.com', parent=self.main)

    def test_return_after_switch_succeeds(self):
        c = _client_for(self.main.user_email)
        c.post('/account/switch/', data=json.dumps({'email': self.sub.user_email}),
               content_type='application/json')
        r = c.post('/account/return/')
        self.assertEqual(r.json()['status'], 'ok')
        self.assertNotIn('acting_as_email', c.session)
        self.assertEqual(_resolve_id(c), self.main.id)

    def test_direct_sub_login_cannot_return_to_main(self):
        c = _client_for(self.sub.user_email)
        r = c.post('/account/return/')
        self.assertEqual(r.status_code, 403)


@override_settings(**_BASE_SETTINGS)
class SubAccountsOverviewAccessTests(TestCase):
    def setUp(self):
        self.main = make_user('mainov@company.com')
        self.sub1 = make_user('sub1ov@company.com', parent=self.main)
        self.other_main = make_user('otherov@company.com')
        self.other_sub = make_user('othersubov@company.com', parent=self.other_main)

    def test_main_sees_only_own_sub_accounts(self):
        c = _client_for(self.main.user_email)
        r = c.get('/sub-accounts/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.sub1.user_email)
        self.assertNotContains(r, self.other_sub.user_email)

    def test_direct_sub_account_denied_overview(self):
        c = _client_for(self.sub1.user_email)
        r = c.get('/sub-accounts/')
        self.assertNotEqual(r.status_code, 200)

    def test_overview_denied_while_acting_as_a_sub_account(self):
        c = _client_for(self.main.user_email)
        c.post('/account/switch/', data=json.dumps({'email': self.sub1.user_email}),
               content_type='application/json')
        r = c.get('/sub-accounts/')
        self.assertNotEqual(r.status_code, 200)

    def test_anonymous_redirected_to_login(self):
        c = Client(SERVER_NAME='127.0.0.1')
        r = c.get('/sub-accounts/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.url)


@override_settings(**_BASE_SETTINGS)
class ActiveAccountResolutionSecurityTests(TestCase):
    """Never trust session['acting_as_email'] blindly."""

    def setUp(self):
        self.main = make_user('mains@company.com')
        self.sub = make_user('subs@company.com', parent=self.main)
        self.unrelated = make_user('unrelateds@company.com')

    def test_tampered_acting_as_email_for_unrelated_account_is_cleared(self):
        c = _client_for(self.main.user_email)
        session = c.session
        session['acting_as_email'] = self.unrelated.user_email  # never went through switch_account
        session.save()

        self.assertEqual(_resolve_id(c), self.main.id)  # falls back safely, no leak

        request = RequestFactory().get('/')
        request.session = c.session
        get_active_user(request)
        self.assertNotIn('acting_as_email', request.session)

    def test_sub_account_cannot_set_its_own_acting_as_email(self):
        c = _client_for(self.sub.user_email)
        session = c.session
        session['acting_as_email'] = self.unrelated.user_email
        session.save()

        self.assertEqual(_resolve_id(c), self.sub.id)

    def test_get_true_user_ignores_acting_as_email(self):
        c = _client_for(self.main.user_email)
        c.post('/account/switch/', data=json.dumps({'email': self.sub.user_email}),
               content_type='application/json')
        request = RequestFactory().get('/')
        request.session = c.session
        self.assertEqual(get_true_user(request).id, self.main.id)
        self.assertEqual(get_active_user(request).id, self.sub.id)


@override_settings(**_BASE_SETTINGS)
class NavContextTests(TestCase):
    def setUp(self):
        self.main = make_user('mainn@company.com')
        self.sub = make_user('subn@company.com', parent=self.main)

    def test_main_sees_switcher_context_with_sub_accounts_listed(self):
        c = _client_for(self.main.user_email)
        r = c.get('/dashboard/')
        self.assertTrue(r.context['nav_is_main_account'])
        self.assertFalse(r.context['nav_is_acting_as'])
        emails = [u.user_email for u in r.context['nav_sub_accounts']]
        self.assertIn(self.sub.user_email, emails)

    def test_acting_as_sub_hides_sibling_list_and_shows_active_email(self):
        c = _client_for(self.main.user_email)
        c.post('/account/switch/', data=json.dumps({'email': self.sub.user_email}),
               content_type='application/json')
        r = c.get('/dashboard/')
        self.assertTrue(r.context['nav_is_acting_as'])
        self.assertEqual(r.context['nav_active_email'], self.sub.user_email)
        self.assertEqual(list(r.context['nav_sub_accounts']), [])

    def test_direct_sub_login_sees_no_switcher(self):
        c = _client_for(self.sub.user_email)
        r = c.get('/dashboard/')
        self.assertFalse(r.context['nav_is_main_account'])
        self.assertFalse(r.context['nav_is_acting_as'])


@override_settings(**_BASE_SETTINGS)
class CreditIsolationTests(TestCase):
    def setUp(self):
        self.main = make_user('mainc@company.com')
        self.sub1 = make_user('sub1c@company.com', parent=self.main)
        self.sub2 = make_user('sub2c@company.com', parent=self.main)
        add_service_credits(self.main.id, 'email_validation', 10_000, ref_type='service_purchase', ref_id='t')
        add_service_credits(self.sub1.id, 'email_validation', 2_000, ref_type='service_purchase', ref_id='t')
        add_service_credits(self.sub2.id, 'email_validation', 500, ref_type='service_purchase', ref_id='t')

    def test_balances_are_fully_independent(self):
        self.assertEqual(get_service_balance(self.main.id, 'email_validation'), 10_000)
        self.assertEqual(get_service_balance(self.sub1.id, 'email_validation'), 2_000)
        self.assertEqual(get_service_balance(self.sub2.id, 'email_validation'), 500)

    def test_deduction_while_switched_affects_only_the_active_sub_account(self):
        c = _client_for(self.main.user_email)
        c.post('/account/switch/', data=json.dumps({'email': self.sub1.user_email}),
               content_type='application/json')
        active_id = _resolve_id(c)
        self.assertEqual(active_id, self.sub1.id)

        deduct_service_credits(active_id, 'email_validation', 500, ref_type='validation', ref_id='x')

        self.assertEqual(get_service_balance(self.sub1.id, 'email_validation'), 1_500)
        self.assertEqual(get_service_balance(self.main.id, 'email_validation'), 10_000)
        self.assertEqual(get_service_balance(self.sub2.id, 'email_validation'), 500)


@override_settings(
    **_BASE_SETTINGS,
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class PaymentOwnershipTests(TestCase):
    """Razorpay is mocked (see test_service_checkout.py's fake_razorpay) --
    no network call is made and no real payment is created.

    Seeds its own CreditPackage pricing rows for every service rather than
    relying on the migration-seeded catalogue: this file's own concern is
    which ACCOUNT a purchase is credited to, not pricing itself (already
    covered by test_service_checkout.py), so it shouldn't be coupled to
    whatever pricing rows happen to exist in a given environment.
    """

    def setUp(self):
        from decimal import Decimal
        from Email_validate_app.models import CreditPackage, SERVICE_KEYS
        for service in SERVICE_KEYS:
            CreditPackage.objects.get_or_create(
                service=service, mode=CreditPackage.MODE_TIER,
                min_qty=1, max_qty=None,
                defaults={'price_usd': Decimal('0.01'), 'is_active': True},
            )

        self.main = make_user('mainp@company.com')
        self.sub1 = make_user('sub1p@company.com', parent=self.main)
        self.client = _client_for(self.main.user_email)
        self.client.post('/account/switch/', data=json.dumps({'email': self.sub1.user_email}),
                          content_type='application/json')

    def test_purchase_while_acting_as_sub_is_owned_by_that_sub_account(self):
        cart = dict(FULL_CART_AT_MINIMUM, email_validation=25_000)
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            self.client.post('/subscription/order/', data=json.dumps({'cart': cart}),
                              content_type='application/json')

        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay()):
            r = self.client.post('/subscription/verify/', data=json.dumps({
                'razorpay_order_id':   'order_TEST123',
                'razorpay_payment_id': 'pay_TEST123',
                'razorpay_signature':  'sig',
            }), content_type='application/json')

        self.assertEqual(r.status_code, 200, r.content)
        order = ServiceOrder.objects.get(order_id='order_TEST123')
        self.assertEqual(order.user_id, self.sub1.id)
        payment = Payment.objects.get(order_id='order_TEST123')
        self.assertEqual(payment.user_id, self.sub1.id)

        self.assertEqual(get_service_balance(self.sub1.id, 'email_validation'), 25_000)
        self.assertEqual(get_service_balance(self.main.id, 'email_validation'), 0)

    def test_main_purchase_when_not_switched_is_owned_by_main(self):
        # Return to Main first -- this client is otherwise switched into sub1.
        self.client.post('/account/return/')
        cart = dict(FULL_CART_AT_MINIMUM, email_validation=10_000)
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay('order_TEST456')):
            self.client.post('/subscription/order/', data=json.dumps({'cart': cart}),
                              content_type='application/json')
        with patch('Email_validate_app.views.credits._razorpay_client',
                   return_value=fake_razorpay('order_TEST456')):
            r = self.client.post('/subscription/verify/', data=json.dumps({
                'razorpay_order_id':   'order_TEST456',
                'razorpay_payment_id': 'pay_TEST456',
                'razorpay_signature':  'sig',
            }), content_type='application/json')

        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ServiceOrder.objects.get(order_id='order_TEST456').user_id, self.main.id)
        self.assertEqual(get_service_balance(self.main.id, 'email_validation'), 10_000)
