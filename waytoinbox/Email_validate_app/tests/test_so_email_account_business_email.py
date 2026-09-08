"""Business-email-only restriction for Sales Outreach sender accounts.

Reuses Email_validate_app.services.email_domain_policy.is_free_email_domain
(already covered by its own unit tests in test_signup_business_email.py) --
this file only covers its wiring into
views.so_email_accounts.so_email_account_action's 'add' branch. The check
sits before the duplicate-email check, credit deduction, and account
creation, so a blocked domain never spends a credit or creates a row.
Existing accounts (added before this restriction, or added directly via
the ORM as in these tests) are never re-validated by 'edit'/'test'.
"""
import json

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import UserTable, SOEmailAccount
from Email_validate_app.services.credit_manager import add_service_credits, get_service_balance


def make_user(email):
    return UserTable.objects.create_user(
        user_name='SO Account Test', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SOEmailAccountBusinessEmailTests(TestCase):

    URL = '/Sales-Outreach/so-accounts/action/'

    def setUp(self):
        self.user = make_user('so_business_email_test@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()
        # Enough credits for every test in this class to add one account.
        add_service_credits(self.user.id, 'sales_outreach', 5,
                             ref_type='service_purchase', ref_id='t')

    def _add(self, email, provider='google', password='apppassword'):
        return self.client.post(self.URL, data=json.dumps({
            'action': 'add', 'email': email, 'provider': provider,
            'display_name': 'Test Sender', 'password': password,
        }), content_type='application/json')

    def _live(self):
        return SOEmailAccount.objects.filter(
            user_id=self.user.id, deleted_at__isnull=True)

    def test_business_domain_allowed(self):
        body = self._add('sender@company.com').json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(self._live().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 4)

    def test_gmail_blocked(self):
        body = self._add('sender@gmail.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('business email', body['message'].lower())

    def test_outlook_blocked(self):
        body = self._add('sender@outlook.com').json()
        self.assertEqual(body['status'], 'error')

    def test_hotmail_blocked(self):
        body = self._add('sender@hotmail.com').json()
        self.assertEqual(body['status'], 'error')

    def test_yahoo_blocked(self):
        body = self._add('sender@yahoo.com').json()
        self.assertEqual(body['status'], 'error')

    def test_mixed_case_domain_blocked(self):
        body = self._add('Sender@GMAIL.COM').json()
        self.assertEqual(body['status'], 'error')

    def test_blocked_domain_creates_no_account_row(self):
        self._add('sender@yahoo.com')
        self.assertEqual(self._live().count(), 0)

    def test_blocked_domain_spends_no_credit(self):
        self._add('sender@yahoo.com')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 5)

    def test_block_applies_regardless_of_chosen_provider(self):
        # provider only selects SMTP/IMAP hosts -- the domain check must
        # fire the same way no matter which one is sent.
        body_google = self._add('sender1@outlook.com', provider='google').json()
        body_microsoft = self._add('sender2@gmail.com', provider='microsoft').json()
        self.assertEqual(body_google['status'], 'error')
        self.assertEqual(body_microsoft['status'], 'error')
        self.assertEqual(self._live().count(), 0)

    def test_existing_free_domain_account_unaffected_by_edit(self):
        # Simulates an account that already existed before this restriction
        # shipped (added directly, bypassing the guarded endpoint) -- 'edit'
        # must keep working on it exactly as before.
        acc = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Old Gmail Sender',
            email='preexisting@gmail.com', smtp_host='smtp.gmail.com', smtp_port=587,
            imap_host='imap.gmail.com', imap_port=993, imap_ssl=True,
            username='preexisting@gmail.com', password='irrelevant', daily_limit=50,
        )
        resp = self.client.post(self.URL, data=json.dumps({
            'action': 'edit', 'id': acc.id, 'daily_limit': 60, 'display_name': 'Renamed',
        }), content_type='application/json')
        body = resp.json()
        self.assertEqual(body['status'], 'ok')
        acc.refresh_from_db()
        self.assertEqual(acc.daily_limit, 60)
        self.assertEqual(acc.display_name, 'Renamed')
        self.assertEqual(acc.email, 'preexisting@gmail.com')
