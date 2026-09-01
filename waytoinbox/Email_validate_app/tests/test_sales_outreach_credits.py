"""Phase 6, commit 3: Sales Outreach account creation gating.

Adding a Sales Outreach email account now costs exactly 1 sales_outreach
credit. There is no legacy pool for this service, so the new ServiceCredit
wallet is the only source and there is no fallback.

Account creation and the deduction share one transaction, so the two either
both happen or neither does. Nothing is ever refunded by hand.

The concurrency cases live in test_sales_outreach_concurrency.py — they need
real threads on committed transactions, which a TestCase's wrapping rollback
would hide.
"""
import json
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, SOEmailAccount,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='SO Test', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SalesOutreachAccountCreditTests(TestCase):

    URL = '/Sales-Outreach/so-accounts/action/'

    def setUp(self):
        self.user = make_user('so_credits@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _add(self, email='a@example.com', provider='google', password='apppassword'):
        return self.client.post(self.URL, data=json.dumps({
            'action': 'add', 'email': email, 'provider': provider,
            'display_name': 'Test', 'password': password,
        }), content_type='application/json')

    def _live(self):
        return SOEmailAccount.objects.filter(
            user_id=self.user.id, deleted_at__isnull=True)

    # 1 ---------------------------------------------------------------------

    def test_successful_creation_costs_one_credit(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')

        body = self._add().json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(self._live().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)

    # 2 ---------------------------------------------------------------------

    def test_no_credit_means_no_account(self):
        body = self._add().json()

        self.assertEqual(body['status'], 'error')
        self.assertIn('credit', body['message'].lower())
        self.assertEqual(self._live().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)

    # 3 ---------------------------------------------------------------------

    def test_exactly_one_credit_per_account(self):
        """The app caps a user at 2 live accounts, so 3 credits are spent by
        creating, soft-deleting and re-creating."""
        add_service_credits(self.user.id, 'sales_outreach', 3,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self._add('a@example.com').json()['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 2)

        self.assertEqual(self._add('b@example.com').json()['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)

        # Free a slot, then add a third distinct mailbox.
        from django.utils.timezone import now
        self._live().filter(email='a@example.com').update(deleted_at=now())

        self.assertEqual(self._add('c@example.com').json()['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)
        self.assertEqual(self._live().count(), 2)

    # 4 ---------------------------------------------------------------------

    def test_insufficient_credits_rejects_and_creates_nothing(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')
        self._add('a@example.com')
        self._add('b@example.com')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)

        from django.utils.timezone import now
        self._live().filter(email='a@example.com').update(deleted_at=now())

        body = self._add('c@example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)
        self.assertFalse(self._live().filter(email='c@example.com').exists())

    # 5 ---------------------------------------------------------------------

    def test_duplicate_account_is_rejected_and_costs_nothing(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self._add('dup@example.com').json()['status'], 'ok')
        second = self._add('dup@example.com').json()

        self.assertEqual(second['status'], 'error')
        self.assertIn('already connected', second['message'])
        self.assertEqual(self._live().filter(email='dup@example.com').count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)

    # 6 ---------------------------------------------------------------------

    def test_duplicate_detection_is_case_insensitive(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self._add('Test@Example.com').json()['status'], 'ok')
        # Stored lower-cased by the view.
        self.assertTrue(self._live().filter(email='test@example.com').exists())

        second = self._add('test@example.com').json()
        self.assertEqual(second['status'], 'error')
        self.assertIn('already connected', second['message'])

        third = self._add('TEST@EXAMPLE.COM').json()
        self.assertEqual(third['status'], 'error')

        self.assertEqual(self._live().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)

    def test_a_soft_deleted_mailbox_can_be_re_added(self):
        """The duplicate guard must not permanently block a removed account —
        this is why no UNIQUE (user, email) index was added."""
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')
        self._add('again@example.com')

        from django.utils.timezone import now
        self._live().update(deleted_at=now())

        self.assertEqual(self._add('again@example.com').json()['status'], 'ok')
        self.assertEqual(self._live().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)

    # 7 ---------------------------------------------------------------------

    def test_creation_failure_rolls_the_credit_back(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(SOEmailAccount.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            with self.assertRaises(RuntimeError):
                self._add('boom@example.com')

        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)
        self.assertEqual(self._live().count(), 0)

    def test_deduction_failure_rolls_the_account_back(self):
        """The other direction: if the charge cannot be taken, the row that was
        already INSERTed must disappear with it."""
        add_service_credits(self.user.id, 'sales_outreach', 5,
                            ref_type='service_purchase', ref_id='t')

        from Email_validate_app.services.credit_manager import InsufficientCredits
        with patch('Email_validate_app.services.credit_manager.deduct_service_credits',
                   side_effect=InsufficientCredits('sales_outreach', 1, 0)):
            body = self._add('rollback@example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertEqual(self._live().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 5)

    # 8 ---------------------------------------------------------------------

    def test_audit_log_entry(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        acc_id = self._add('audit@example.com').json()['id']

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'sales_outreach')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'sales_outreach_account')
        self.assertEqual(entry.ref_id, str(acc_id))
        self.assertEqual(entry.description, 'Sales Outreach email account')

    def test_rejected_add_writes_no_audit_entry(self):
        self._add('nope@example.com')
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)

    # 9 ---------------------------------------------------------------------

    def test_no_legacy_wallet_is_touched(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._add('legacy@example.com')

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.ac_current_credits, 50)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(
            sorted(ServiceCredit.objects.filter(user_id=self.user.id)
                   .values_list('service', flat=True)),
            ['sales_outreach'])

    def test_legacy_credits_are_no_substitute_for_sales_outreach(self):
        """There is no fallback: a fat legacy balance must not buy an account."""
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=99999,
                                      ac_current_credits=999, cc_current_credits=999)

        body = self._add('nofallback@example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertEqual(self._live().count(), 0)
        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(
            (row.vc_current_credits, row.ac_current_credits, row.cc_current_credits),
            (99999, 999, 999))

    # preserved behaviour ---------------------------------------------------

    def test_two_account_limit_still_applies_and_costs_nothing(self):
        add_service_credits(self.user.id, 'sales_outreach', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add('a@example.com')
        self._add('b@example.com')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 3)

        body = self._add('c@example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertIn('up to 2', body['message'])
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 3)
        self.assertEqual(self._live().count(), 2)

    def test_validation_errors_cost_nothing(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')

        bad_email = self._add('not-an-email').json()
        self.assertEqual(bad_email['status'], 'error')
        self.assertIn('valid email', bad_email['message'])

        no_pwd = self._add('ok@example.com', password='').json()
        self.assertEqual(no_pwd['status'], 'error')
        self.assertIn('App password', no_pwd['message'])

        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 2)
        self.assertEqual(self._live().count(), 0)

    def test_provider_and_credentials_handling_unchanged(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')

        self._add('ms@example.com', provider='microsoft')
        acc = self._live().get(email='ms@example.com')
        self.assertEqual(acc.provider, 'microsoft')
        self.assertEqual(acc.smtp_host, 'smtp.office365.com')
        self.assertEqual(acc.imap_host, 'outlook.office365.com')
        self.assertEqual(acc.username, 'ms@example.com')

        # Password is still stored signed, never in the clear.
        from django.core import signing
        self.assertNotEqual(acc.password, 'apppassword')
        self.assertEqual(signing.loads(acc.password, salt='so-ea-pwd'), 'apppassword')

    def test_spaces_are_still_stripped_from_the_app_password(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        self._add('sp@example.com', password='abcd efgh ijkl mnop')

        from django.core import signing
        acc = self._live().get(email='sp@example.com')
        self.assertEqual(signing.loads(acc.password, salt='so-ea-pwd'),
                         'abcdefghijklmnop')

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, data=json.dumps({
            'action': 'add', 'email': 'x@example.com', 'password': 'p',
        }), content_type='application/json')
        self.assertIn(r.status_code, (302, 401))
        self.assertEqual(SOEmailAccount.objects.filter(email='x@example.com').count(), 0)
