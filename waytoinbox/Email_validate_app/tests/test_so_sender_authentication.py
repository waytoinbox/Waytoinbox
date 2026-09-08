"""Sender email account activation and sending eligibility.

FINAL RULE (supersedes the original "all three of SPF/DKIM/DMARC must pass"
design this file originally tested):

  - Connection status (SOEmailAccount.is_connected()) reflects ONLY the
    mailbox's own SMTP/IMAP credentials. SPF/DKIM/DMARC must never change
    it -- a domain that fails all three domain-authentication checks is
    still "Active"/Connected as long as the app password is valid.

  - Sending eligibility (SOEmailAccount.is_sending_eligible()) is the real
    gate for actually sending: is_connected() AND spf_status == 'pass'.
    DKIM and DMARC are still checked, stored, and displayed, but do NOT
    block sending on their own.

  - is_authenticated() (SPF AND DKIM AND DMARC all 'pass') is kept for its
    original "every domain-authentication check passed" display/semantics
    only -- it must never again be used to gate sending or the Connection
    badge; is_sending_eligible() is used everywhere sending eligibility
    actually matters.

Reuses the existing DNS checker (services/dmarc_checker.py::check_spf/
check_dmarc/check_dkim_auto/detect_mx_provider) unchanged -- these tests
patch it rather than hitting real DNS, exactly the same way
test_so_drip_send.py patches services/so_smtp.py::open_smtp instead of
opening a real SMTP connection.

Covers:
  1. SOEmailAccount.is_authenticated() / is_connected() / is_sending_eligible().
  2. views/so_email_accounts.py's 'add' action -- App Password is validated
     through a real SMTP attempt, never trusted just for being present.
  3. views/so_email_accounts.py's 'test' action (also driving the always-
     visible Reconnect button) -- runs MX/SPF/DKIM/DMARC and returns both
     'active' (connection-only) and 'sending_eligible' (connection+SPF).
  4. Send protection -- services/so_drip.py::send_next_step enforces
     is_sending_eligible(), including the "became ineligible after
     assignment" transition.
  5. Enrollment gate -- tasks/so_send_campaign.py::so_send_campaign_task
     only ever assigns a new contact to a sending-eligible rotation account.
  6. New Campaign -> Settings -> Send From only offers sending-eligible
     accounts (views/so_sender.py::_new_campaign_context).
"""
import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, SOCampaign, SOCampaignContact, SOProspect, SOEmailAccount,
    SOEmailAccountRotation, SOList, SOListProspect, SOSequenceStep, SOSequenceVariant,
)
from Email_validate_app.services import so_drip
from Email_validate_app.tasks.so_send_campaign import so_send_campaign_task

_BASE_SETTINGS = dict(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Sender Auth Test', user_email=email, password='StrongPass123!')


def make_account(user, email, spf='pass', dkim='pass', dmarc='pass', status='connected'):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=50, status=status,
        spf_status=spf, dkim_status=dkim, dmarc_status=dmarc,
    )


class IsAuthenticatedModelTests(TestCase):
    """Unit coverage for SOEmailAccount.is_authenticated() -- kept exactly
    as originally built (all three must pass); this concept still exists,
    it's just no longer used for sending eligibility."""

    def setUp(self):
        self.user = make_user('is-authenticated-model@example.com')

    def test_all_pass_is_authenticated(self):
        acc = make_account(self.user, 'all-pass@example.com', 'pass', 'pass', 'pass')
        self.assertTrue(acc.is_authenticated())

    def test_spf_fail_is_not_authenticated(self):
        acc = make_account(self.user, 'spf-fail@example.com', 'fail', 'pass', 'pass')
        self.assertFalse(acc.is_authenticated())

    def test_dkim_fail_is_not_authenticated(self):
        acc = make_account(self.user, 'dkim-fail@example.com', 'pass', 'fail', 'pass')
        self.assertFalse(acc.is_authenticated())

    def test_dmarc_fail_is_not_authenticated(self):
        acc = make_account(self.user, 'dmarc-fail@example.com', 'pass', 'pass', 'fail')
        self.assertFalse(acc.is_authenticated())

    def test_multiple_failures_is_not_authenticated(self):
        acc = make_account(self.user, 'multi-fail@example.com', 'fail', 'fail', 'pass')
        self.assertFalse(acc.is_authenticated())

    def test_unchecked_default_is_not_authenticated(self):
        acc = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Fresh',
            email='fresh@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='fresh@example.com',
            password='x', daily_limit=50,
        )
        self.assertEqual(acc.spf_status, 'unchecked')
        self.assertFalse(acc.is_authenticated())


class IsConnectedModelTests(TestCase):
    """Unit coverage for SOEmailAccount.is_connected() -- SMTP/IMAP only,
    completely independent of SPF/DKIM/DMARC."""

    def setUp(self):
        self.user = make_user('is-connected-model@example.com')

    def test_connected_status_is_connected_even_with_all_auth_failing(self):
        """The exact example from the requirement: SMTP/IMAP = Connected,
        SPF = Fail, DKIM = Fail, DMARC = Fail -> Connection must still be
        Active."""
        acc = make_account(self.user, 'connected-all-fail@example.com',
                            spf='fail', dkim='fail', dmarc='fail', status='connected')
        self.assertTrue(acc.is_connected())

    def test_failed_status_is_not_connected_even_with_all_auth_passing(self):
        acc = make_account(self.user, 'failed-all-pass@example.com',
                            spf='pass', dkim='pass', dmarc='pass', status='failed')
        self.assertFalse(acc.is_connected())

    def test_unchecked_status_is_not_connected(self):
        acc = make_account(self.user, 'unchecked-status@example.com', status='unchecked')
        self.assertFalse(acc.is_connected())


class IsSendingEligibleModelTests(TestCase):
    """Unit coverage for SOEmailAccount.is_sending_eligible() -- every
    example scenario from the requirement, verbatim."""

    def setUp(self):
        self.user = make_user('is-sending-eligible-model@example.com')

    def test_active_spf_pass_dkim_fail_dmarc_fail_is_eligible(self):
        acc = make_account(self.user, 'e1@example.com', spf='pass', dkim='fail', dmarc='fail', status='connected')
        self.assertTrue(acc.is_sending_eligible())

    def test_active_spf_fail_dkim_pass_dmarc_pass_is_not_eligible(self):
        acc = make_account(self.user, 'e2@example.com', spf='fail', dkim='pass', dmarc='pass', status='connected')
        self.assertFalse(acc.is_sending_eligible())

    def test_active_spf_unchecked_dkim_pass_dmarc_pass_is_not_eligible(self):
        acc = make_account(self.user, 'e3@example.com', spf='unchecked', dkim='pass', dmarc='pass', status='connected')
        self.assertFalse(acc.is_sending_eligible())

    def test_inactive_spf_pass_is_not_eligible(self):
        acc = make_account(self.user, 'e4@example.com', spf='pass', status='failed')
        self.assertFalse(acc.is_sending_eligible())

    def test_active_spf_pass_all_pass_is_eligible(self):
        acc = make_account(self.user, 'e5@example.com', spf='pass', dkim='pass', dmarc='pass', status='connected')
        self.assertTrue(acc.is_sending_eligible())


@override_settings(**_BASE_SETTINGS)
class AccountTestActionAuthCheckTests(TestCase):
    """views/so_email_accounts.py's 'test' action (also what the always-
    visible Reconnect button calls) -- runs MX/SPF/DKIM/DMARC via the
    existing dmarc_checker (patched here, never hit over real DNS) and
    returns 'active' (connection-only) and 'sending_eligible' (connection
    AND SPF) as two clearly separate fields."""

    URL = '/Sales-Outreach/so-accounts/action/'

    def setUp(self):
        self.user = make_user('test-action-auth@example.com')
        from django.core import signing
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Sender',
            email='testaction@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='testaction@example.com',
            password=signing.dumps('app-password', salt='so-ea-pwd'), daily_limit=50,
        )
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _post_test(self):
        return self.client.post(self.URL, data=json.dumps({'action': 'test', 'id': self.account.id}),
                                 content_type='application/json')

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Google Workspace / Gmail'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'pass'})
    def test_all_pass_marks_connected_and_sending_eligible(self, mock_spf, mock_dmarc, mock_dkim, mock_mx):
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__.return_value = MagicMock()
            r = self._post_test()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['spf_status'], 'pass')
        self.assertEqual(d['dkim_status'], 'pass')
        self.assertEqual(d['dmarc_status'], 'pass')
        self.assertEqual(d['mx_provider'], 'Google Workspace / Gmail')
        self.assertTrue(d['active'])
        self.assertTrue(d['sending_eligible'])
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_authenticated())
        self.assertTrue(self.account.is_connected())
        self.assertTrue(self.account.is_sending_eligible())
        mock_spf.assert_called_once_with('example.com')
        mock_dmarc.assert_called_once_with('example.com')
        mock_dkim.assert_called_once_with('example.com')
        mock_mx.assert_called_once_with('example.com')

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Other'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'fail', 'reason': 'No SPF record found'})
    def test_spf_fail_keeps_connection_active_but_blocks_sending_eligibility(self, mock_spf, mock_dmarc, mock_dkim, mock_mx):
        """THE core requirement: SMTP/IMAP = Connected, SPF = Fail, DKIM =
        Pass, DMARC = Pass -> Connection status must still be Active, but
        sending eligibility must be False."""
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__.return_value = MagicMock()
            r = self._post_test()
        d = r.json()
        self.assertEqual(d['result'], 'connected')
        self.assertEqual(d['spf_status'], 'fail')
        self.assertTrue(d['active'])              # Connection still Active
        self.assertFalse(d['sending_eligible'])   # but not eligible to send
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_connected())
        self.assertFalse(self.account.is_sending_eligible())
        self.assertFalse(self.account.is_authenticated())

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Other'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'fail'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'fail'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'pass'})
    def test_dkim_and_dmarc_fail_alone_does_not_block_sending_eligibility(self, mock_spf, mock_dmarc, mock_dkim, mock_mx):
        """SMTP/IMAP = Active, SPF = Pass, DKIM = Fail, DMARC = Fail ->
        Sending allowed (per the requirement's own example)."""
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__.return_value = MagicMock()
            r = self._post_test()
        d = r.json()
        self.assertTrue(d['active'])
        self.assertTrue(d['sending_eligible'])
        self.assertFalse(d['dkim_status'] == 'pass')
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_sending_eligible())
        self.assertFalse(self.account.is_authenticated())  # still shown/stored as failing

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Other'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'pass'})
    def test_smtp_failure_still_runs_and_records_auth_check(self, mock_spf, mock_dmarc, mock_dkim, mock_mx):
        """Domain authentication is independent of the SMTP credential
        check -- even when SMTP login fails, MX/SPF/DKIM/DMARC are still
        checked and persisted, but both 'active' and 'sending_eligible'
        are correctly False since the mailbox itself isn't connected."""
        with patch('smtplib.SMTP', side_effect=OSError('connection refused')):
            r = self._post_test()
        d = r.json()
        self.assertEqual(d['result'], 'failed')
        self.assertEqual(d['spf_status'], 'pass')
        self.assertFalse(d['active'])
        self.assertFalse(d['sending_eligible'])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, 'failed')
        self.assertTrue(self.account.is_authenticated())
        self.assertFalse(self.account.is_connected())
        self.assertFalse(self.account.is_sending_eligible())

    def test_smtp_error_message_never_contains_the_app_password(self):
        """Security requirement: the App Password must never be exposed in
        an error message, log, UI, or API response."""
        with patch('smtplib.SMTP', side_effect=OSError('a very specific connection error')):
            r = self._post_test()
        d = r.json()
        self.assertNotIn('app-password', (d.get('error_msg') or '').lower())


@override_settings(**_BASE_SETTINGS)
class AddAccountConnectionValidationTests(TestCase):
    """views/so_email_accounts.py's 'add' action -- the App Password is
    validated through a real SMTP attempt at add time, not just trusted
    for being present."""

    URL = '/Sales-Outreach/so-accounts/action/'

    def setUp(self):
        self.user = make_user('add-account-validation@example.com')
        from Email_validate_app.services.credit_manager import add_service_credits
        add_service_credits(self.user.id, 'sales_outreach', 5, ref_type='service_purchase', ref_id='t')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _add(self, email='newaccount@company.com', password='correct horse battery staple'):
        return self.client.post(self.URL, data=json.dumps({
            'action': 'add', 'provider': 'google', 'display_name': 'Test', 'email': email, 'password': password,
        }), content_type='application/json')

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Other'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'pass'})
    def test_correct_app_password_connects_successfully(self, *mocks):
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__.return_value = MagicMock()
            r = self._add()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['status'], 'ok')
        self.assertEqual(d['result'], 'connected')
        self.assertTrue(d['active'])
        acc = SOEmailAccount.objects.get(id=d['id'])
        self.assertEqual(acc.status, 'connected')
        self.assertTrue(acc.is_connected())

    @patch('Email_validate_app.services.dmarc_checker.detect_mx_provider', return_value={'status': 'pass', 'provider': 'Other'})
    @patch('Email_validate_app.services.dmarc_checker.check_dkim_auto', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_dmarc', return_value={'status': 'pass'})
    @patch('Email_validate_app.services.dmarc_checker.check_spf', return_value={'status': 'pass'})
    def test_incorrect_app_password_is_not_activated(self, *mocks):
        import smtplib as smtplib_mod
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_server = MagicMock()
            mock_server.__enter__.return_value = mock_server
            mock_server.login.side_effect = smtplib_mod.SMTPAuthenticationError(535, b'Invalid credentials')
            mock_smtp_cls.return_value = mock_server
            r = self._add(email='badpassword@company.com')
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['status'], 'ok')  # the row itself still saves...
        self.assertEqual(d['result'], 'failed')
        self.assertFalse(d['active'])
        self.assertIn('Authentication failed', d['error_msg'])
        acc = SOEmailAccount.objects.get(id=d['id'])
        self.assertEqual(acc.status, 'failed')
        self.assertFalse(acc.is_connected())
        self.assertFalse(acc.is_sending_eligible())

    def test_missing_password_is_rejected_before_any_smtp_attempt(self):
        r = self._add(password='')
        d = r.json()
        self.assertEqual(d['status'], 'error')
        self.assertNotIn('id', d)


class SendProtectionTests(TestCase):
    """Send-time enforcement: services/so_drip.py::send_next_step enforces
    is_sending_eligible() (connection AND SPF), server-side, regardless of
    what the UI shows -- and DKIM/DMARC never block it on their own."""

    def setUp(self):
        self.user = make_user('send-protection@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Auth Gate Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
        )
        SOSequenceStep.objects.create(campaign=self.campaign, order=1, wait_days=0, wait_hours=0)
        step = self.campaign.steps.get(order=1)
        SOSequenceVariant.objects.create(
            step=step, label='A', subject='Hello', html_body='<p>x</p>', weight=100, is_active=True,
        )
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='auth-gate-recipient@example.com', first_name='T', last_name='P',
            status='subscribed',
        )

    def _contact_for(self, account):
        return SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=account, status='sending', current_step=1, attempts=0,
        )

    def test_spf_fail_account_cannot_send(self):
        account = make_account(self.user, 'spf-fail-sender@example.com', spf='fail')
        cc = self._contact_for(account)
        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        mock_open_smtp.assert_not_called()
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'active')
        self.assertEqual(cc.attempts, 1)

    def test_spf_fail_account_eventually_fails_permanently(self):
        account = make_account(self.user, 'spf-fail-sender-2@example.com', spf='fail')
        cc = self._contact_for(account)
        cc.attempts = so_drip.MAX_ATTEMPTS - 1
        cc.save(update_fields=['attempts'])
        with patch('Email_validate_app.services.so_smtp.open_smtp'):
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'failed')
        self.assertIn('eligible', cc.error)

    def test_disconnected_account_cannot_send(self):
        """SMTP/IMAP = Inactive, SPF = Pass -> Sending NOT allowed."""
        account = make_account(self.user, 'disconnected-sender@example.com', spf='pass', status='failed')
        cc = self._contact_for(account)
        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        mock_open_smtp.assert_not_called()

    def test_spf_unchecked_cannot_send(self):
        """SMTP/IMAP = Active, SPF = Unchecked -> Sending NOT allowed."""
        account = make_account(self.user, 'spf-unchecked-sender@example.com', spf='unchecked')
        cc = self._contact_for(account)
        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        mock_open_smtp.assert_not_called()

    def test_dkim_and_dmarc_fail_does_not_block_sending(self):
        """SMTP/IMAP = Active, SPF = Pass, DKIM = Fail, DMARC = Fail ->
        Sending allowed."""
        account = make_account(self.user, 'dkim-dmarc-fail-sender@example.com',
                                spf='pass', dkim='fail', dmarc='fail')
        cc = self._contact_for(account)
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(cc)
        self.assertTrue(result)
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'completed')

    def test_active_account_can_still_send(self):
        """Regression: a fully-eligible account must keep sending exactly
        as before this gate existed."""
        account = make_account(self.user, 'active-sender@example.com')
        cc = self._contact_for(account)
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(cc)
        self.assertTrue(result)
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'completed')

    def test_account_that_becomes_spf_fail_after_assignment_cannot_send_next_step(self):
        """Sticky-account transition: the account was fine (SPF pass) at
        assignment time, then degrades to SPF fail before the NEXT send --
        send_next_step must still catch it and stop using it."""
        account = make_account(self.user, 'degrades-spf@example.com', spf='pass')
        cc = self._contact_for(account)  # account already assigned (sticky)

        account.spf_status = 'fail'
        account.save(update_fields=['spf_status'])

        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        mock_open_smtp.assert_not_called()
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'active')

    def test_account_that_becomes_disconnected_after_assignment_cannot_send_next_step(self):
        """Same transition, but the mailbox itself disconnects (e.g. the
        app password was revoked) instead of SPF degrading."""
        account = make_account(self.user, 'degrades-connection@example.com', status='connected')
        cc = self._contact_for(account)

        account.status = 'failed'
        account.save(update_fields=['status'])

        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)
        self.assertFalse(result)
        mock_open_smtp.assert_not_called()
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'active')


@override_settings(**_BASE_SETTINGS)
class EnrollmentGateTests(TestCase):
    """tasks/so_send_campaign.py::so_send_campaign_task must only ever
    assign a new contact to a sending-eligible rotation account (connected
    AND SPF pass) -- account rotation must never select an ineligible
    account, and DKIM/DMARC failing alone must never exclude one."""

    def setUp(self):
        self.user = make_user('enrollment-gate@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Enrollment Gate Campaign', subject='s', html_body='<p>x</p>',
            status='sending',
        )
        SOSequenceStep.objects.create(campaign=self.campaign, order=1, wait_days=0, wait_hours=0)
        so_list = SOList.objects.create(user=self.user, name='Enrollment List')
        self.campaign.recipient_lists.add(so_list)
        prospect = SOProspect.objects.create(
            user_id=self.user.id, email='enroll-me@example.com', first_name='T', last_name='P',
            status='subscribed',
        )
        SOListProspect.objects.create(so_list=so_list, prospect=prospect)

    def test_only_spf_pass_rotation_is_used(self):
        bad = make_account(self.user, 'enroll-bad@example.com', spf='fail')
        good = make_account(self.user, 'enroll-good@example.com')
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=bad, order=1)
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=good, order=2)

        result = so_send_campaign_task(self.campaign.id)

        self.assertNotEqual(result['status'], 'no_account')
        cc = SOCampaignContact.objects.get(campaign=self.campaign, email='enroll-me@example.com')
        self.assertEqual(cc.account_id, good.id)

    def test_all_rotations_spf_fail_yields_no_account(self):
        bad = make_account(self.user, 'enroll-onlybad@example.com', spf='fail')
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=bad, order=1)

        result = so_send_campaign_task(self.campaign.id)

        self.assertEqual(result['status'], 'no_account')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, 'failed')

    def test_rotation_with_dkim_dmarc_fail_but_spf_pass_is_still_used(self):
        """Account rotation never excludes an account for DKIM/DMARC
        failing alone -- only SPF (and connection) matter here."""
        acc = make_account(self.user, 'enroll-dkim-dmarc-fail@example.com',
                            spf='pass', dkim='fail', dmarc='fail')
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=acc, order=1)

        result = so_send_campaign_task(self.campaign.id)

        self.assertNotEqual(result['status'], 'no_account')
        cc = SOCampaignContact.objects.get(campaign=self.campaign, email='enroll-me@example.com')
        self.assertEqual(cc.account_id, acc.id)


@override_settings(**_BASE_SETTINGS)
class SendFromDropdownTests(TestCase):
    """New Campaign -> Settings -> Send From only ever offers sending-
    eligible accounts (views/so_sender.py::_new_campaign_context)."""

    URL = '/Sales-Outreach/sender/create/'

    def setUp(self):
        self.user = make_user('send-from-dropdown@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_dropdown_only_contains_connected_and_spf_pass_accounts(self):
        shown = make_account(self.user, 'email1@gmail.com', spf='pass', status='connected')
        hidden_spf_fail = make_account(self.user, 'email2@gmail.com', spf='fail', status='connected')
        hidden_disconnected = make_account(self.user, 'email3@gmail.com', spf='pass', status='failed')
        hidden_spf_unchecked = make_account(self.user, 'email4@gmail.com', spf='unchecked', status='connected')

        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        shown_ids = {a.id for a in r.context['email_accounts']}

        self.assertIn(shown.id, shown_ids)
        self.assertNotIn(hidden_spf_fail.id, shown_ids)
        self.assertNotIn(hidden_disconnected.id, shown_ids)
        self.assertNotIn(hidden_spf_unchecked.id, shown_ids)

    def test_dkim_dmarc_fail_account_still_shown_when_spf_passes(self):
        acc = make_account(self.user, 'dkim-dmarc-fail@gmail.com', spf='pass', dkim='fail', dmarc='fail')
        r = self.client.get(self.URL)
        shown_ids = {a.id for a in r.context['email_accounts']}
        self.assertIn(acc.id, shown_ids)


@override_settings(**_BASE_SETTINGS)
class ReconnectButtonTests(TestCase):
    """The Reconnect button must ALWAYS be visible on the Email Accounts
    page, regardless of the account's current status."""

    URL = '/Sales-Outreach/so-accounts/'

    def setUp(self):
        self.user = make_user('reconnect-visibility@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _reconnect_button_count(self, body):
        return body.count('soea-reconnect-btn')

    def test_reconnect_visible_for_connected_account(self):
        make_account(self.user, 'connected@example.com', status='connected')
        r = self.client.get(self.URL)
        self.assertGreaterEqual(self._reconnect_button_count(r.content.decode()), 1)

    def test_reconnect_visible_for_failed_account(self):
        make_account(self.user, 'failed@example.com', status='failed')
        r = self.client.get(self.URL)
        self.assertGreaterEqual(self._reconnect_button_count(r.content.decode()), 1)

    def test_reconnect_visible_for_unchecked_account(self):
        make_account(self.user, 'unchecked@example.com', status='unchecked')
        r = self.client.get(self.URL)
        self.assertGreaterEqual(self._reconnect_button_count(r.content.decode()), 1)

    def test_reconnect_always_calls_the_full_recheck_not_the_password_modal(self):
        """Reconnect's onclick must always be checkAccount(...) -- it must
        never be wired straight to openUpdatePwd(...) based on status."""
        acc = make_account(self.user, 'failed-btn@example.com', status='failed')
        r = self.client.get(self.URL)
        body = r.content.decode()
        self.assertIn('onclick="checkAccount({0}, this)"'.format(acc.id), body)

    def test_update_app_password_is_a_separate_always_available_menu_item(self):
        acc = make_account(self.user, 'has-menu-item@example.com', status='connected')
        r = self.client.get(self.URL)
        body = r.content.decode()
        self.assertIn("openUpdatePwd({0}".format(acc.id), body)
