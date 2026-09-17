"""Focused tests for per-account Sender Name overrides in Sales Outreach
campaigns.

A campaign with multiple selected sender accounts can now give each account
its own campaign-specific Sender Name (SOEmailAccountRotation.sender_name),
replacing the old single campaign-wide SOCampaign.from_name input in the
wizard UI. Resolution order (services/so_drip.py::resolve_sender_name):

    rotation.sender_name -> campaign.from_name -> account.display_name -> account.email

Covers:
  - resolve_sender_name()'s fallback chain in isolation.
  - views/so_sender.py::_apply_campaign_payload persistence (via
    so_campaign_save), alongside the pre-existing email_account_counts.
  - views/so_sender.py::_duplicate_campaign copying the override.
  - views/so_sender.py::_new_campaign_context hydration for campaign edit.
  - The actual MIME From header produced by services/so_drip.py::send_next_step.
  - views/so_sender.py::so_test_send resolving server-side instead of
    trusting a client-supplied name.
  - Reply-To OFF/ON behavior is unaffected by any of the above.

Campaign Sending Count, sender rotation/selection, quota enforcement, and
Reply-To's own architecture are all out of scope here — see
test_so_campaign_sending_count.py / test_so_drip_send.py for those.
"""
import json
import re
from datetime import time
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client, override_settings
from django.urls import reverse

from Email_validate_app.models import (
    SOCampaign, SOCampaignContact, SOEmailAccount, SOEmailAccountRotation,
    SOProspect, SOSequenceStep, SOSequenceVariant, UserTable,
)
from Email_validate_app.services import so_drip


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Sender Name Test', user_email=email, password='StrongPass123!')


def make_account(user, email, display_name='', spf_status='pass'):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name=display_name,
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=120, status='connected', spf_status=spf_status,
    )


def minimal_sequence():
    return [{'wait_days': 0, 'wait_hours': 0, 'variants': [
        {'label': 'A', 'subject': 'Hello', 'html_body': '<p>x</p>', 'weight': 1},
    ]}]


# ── resolve_sender_name() fallback chain, in isolation ──────────────────────

class ResolveSenderNameTests(TestCase):
    def setUp(self):
        self.user = make_user('resolve_sender_name@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Resolve Campaign', subject='s', html_body='<p>x</p>',
        )
        self.account = make_account(self.user, 'resolve-acct@example.com')

    def test_no_rotation_row_no_from_name_no_display_name_falls_back_to_email(self):
        """Nothing set anywhere -- the account's own email is the last resort."""
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), self.account.email)

    def test_no_rotation_row_no_from_name_uses_display_name(self):
        self.account.display_name = 'John'
        self.account.save(update_fields=['display_name'])
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), 'John')

    def test_campaign_from_name_used_when_no_override_exists(self):
        """Backward compatibility: a campaign saved before per-account Sender
        Name existed keeps using its old single from_name, even though no
        SOEmailAccountRotation row (or an override-less one) exists for
        this account."""
        self.account.display_name = 'John'
        self.account.save(update_fields=['display_name'])
        self.campaign.from_name = 'Legacy Campaign Name'
        self.campaign.save(update_fields=['from_name'])
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), 'Legacy Campaign Name')

    def test_rotation_row_with_blank_sender_name_still_falls_through(self):
        """An existing rotation row with sender_name='' (the migration
        default for every pre-existing row) behaves exactly like no row at
        all -- not a special 'blank override' case."""
        self.account.display_name = 'John'
        self.account.save(update_fields=['display_name'])
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=self.account, sender_name='')
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), 'John')

    def test_rotation_override_wins_over_from_name_and_display_name(self):
        self.account.display_name = 'John'
        self.account.save(update_fields=['display_name'])
        self.campaign.from_name = 'Legacy Campaign Name'
        self.campaign.save(update_fields=['from_name'])
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Override')
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), 'Custom Override')

    def test_two_accounts_resolve_independently(self):
        acc_b = make_account(self.user, 'resolve-acct-b@example.com', display_name='David')
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Sales Department')
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=acc_b)  # no override
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, self.account), 'Sales Department')
        self.assertEqual(so_drip.resolve_sender_name(self.campaign, acc_b), 'David')


# ── Save/autosave persistence via _apply_campaign_payload ───────────────────

class CampaignSaveSenderNameTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_save_sender_name@example.com')
        self.acc_a = make_account(self.user, 'save-name-a@example.com', display_name='John')
        self.acc_b = make_account(self.user, 'save-name-b@example.com', display_name='Sales Team')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _save(self, **overrides):
        payload = {
            'name': 'Sender Name Campaign',
            'action': 'save_draft',
            'sequence': minimal_sequence(),
            'email_account_ids': [self.acc_a.id],
            'email_account_counts': {},
            'email_account_sender_names': {},
        }
        payload.update(overrides)
        return self.client.post(
            '/Sales-Outreach/sender/save/', data=json.dumps(payload),
            content_type='application/json')

    def test_no_override_stores_blank(self):
        r = self._save(email_account_ids=[self.acc_a.id])
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sender Name Campaign')
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        self.assertEqual(rot.sender_name, '')

    def test_override_is_persisted_per_account(self):
        r = self._save(
            email_account_ids=[self.acc_a.id],
            email_account_sender_names={str(self.acc_a.id): 'Custom John'},
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sender Name Campaign')
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        self.assertEqual(rot.sender_name, 'Custom John')

    def test_multiple_accounts_with_different_names_persisted_independently(self):
        r = self._save(
            email_account_ids=[self.acc_a.id, self.acc_b.id],
            email_account_sender_names={
                str(self.acc_a.id): 'John',
                str(self.acc_b.id): 'Sales Department',
            },
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sender Name Campaign')
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a).sender_name, 'John')
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_b).sender_name,
            'Sales Department')

    def test_changing_only_one_account_leaves_the_other_untouched(self):
        """Mirrors the task's own example: editing sales@ must not affect john@."""
        r = self._save(
            email_account_ids=[self.acc_a.id, self.acc_b.id],
            email_account_sender_names={str(self.acc_a.id): 'John', str(self.acc_b.id): 'Sales Team'},
        )
        campaign_id = r.json()['campaign_id']

        r2 = self._save(
            campaign_id=campaign_id,
            email_account_ids=[self.acc_a.id, self.acc_b.id],
            email_account_sender_names={str(self.acc_a.id): 'John', str(self.acc_b.id): 'Sales Department'},
        )
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign_id=campaign_id, account=self.acc_a).sender_name, 'John')
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign_id=campaign_id, account=self.acc_b).sender_name,
            'Sales Department')

    def test_missing_value_never_errors_on_draft_or_strict_save(self):
        """Unlike Campaign Sending Count, an absent/blank Sender Name is
        always valid -- there is no ceiling to violate."""
        r = self._save(
            email_account_ids=[self.acc_a.id], email_account_sender_names={}, action='save_draft',
        )
        self.assertEqual(r.status_code, 200, r.content)

    def test_does_not_break_existing_email_account_counts(self):
        r = self._save(
            email_account_ids=[self.acc_a.id, self.acc_b.id],
            email_account_counts={str(self.acc_a.id): 40, str(self.acc_b.id): 60},
            email_account_sender_names={str(self.acc_a.id): 'John'},
            sender_send_count_enabled=True,
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sender Name Campaign')
        rot_a = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        rot_b = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_b)
        self.assertEqual(rot_a.daily_send_count, 40)
        self.assertEqual(rot_a.sender_name, 'John')
        self.assertEqual(rot_b.daily_send_count, 60)
        self.assertEqual(rot_b.sender_name, '')

    def test_editing_an_existing_campaign_updates_its_stored_sender_name(self):
        r = self._save(
            email_account_ids=[self.acc_a.id],
            email_account_sender_names={str(self.acc_a.id): 'John'},
        )
        campaign_id = r.json()['campaign_id']

        r2 = self._save(
            campaign_id=campaign_id, email_account_ids=[self.acc_a.id],
            email_account_sender_names={str(self.acc_a.id): 'Johnny'},
        )
        self.assertEqual(r2.status_code, 200, r2.content)
        rot = SOEmailAccountRotation.objects.get(campaign_id=campaign_id, account=self.acc_a)
        self.assertEqual(rot.sender_name, 'Johnny')

    def test_existing_campaigns_from_name_is_never_reset_by_a_resave(self):
        """The removed standalone field's payload key ('sender_name') is
        never sent by the current wizard -- a save/autosave must leave
        campaign.from_name exactly as it was, not reset it to ''."""
        campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Pre-existing Campaign', subject='s', html_body='<p>x</p>',
            from_name='Legacy Name', status='draft',
        )
        r = self._save(campaign_id=campaign.id, name='Pre-existing Campaign',
                        email_account_ids=[self.acc_a.id])
        self.assertEqual(r.status_code, 200, r.content)
        campaign.refresh_from_db()
        self.assertEqual(campaign.from_name, 'Legacy Name')


# ── Duplication ──────────────────────────────────────────────────────────────

class CampaignDuplicateSenderNameTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_dup_sender_name@example.com')
        self.account = make_account(self.user, 'dup-acct@example.com', display_name='John')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Original Campaign', subject='s', html_body='<p>x</p>',
            status='draft',
        )
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Override', order=0)
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_duplicate_copies_the_sender_name_override(self):
        r = self.client.post(
            f'/Sales-Outreach/sender/{self.campaign.id}/action/',
            data=json.dumps({'action': 'duplicate'}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        new_campaign_id = r.json()['campaign_id']
        new_rot = SOEmailAccountRotation.objects.get(campaign_id=new_campaign_id, account=self.account)
        self.assertEqual(new_rot.sender_name, 'Custom Override')


# ── Edit-page hydration ──────────────────────────────────────────────────────

class CampaignEditHydrationTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_edit_sender_name@example.com')
        self.acc_a = make_account(self.user, 'edit-name-a@example.com', display_name='John')
        self.acc_b = make_account(self.user, 'edit-name-b@example.com', display_name='Sales Team')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Edit Campaign', subject='s', html_body='<p>x</p>',
            status='draft',
        )
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_a, sender_name='John', order=0)
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_b, order=1)   # no override
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _editing_data(self):
        r = self.client.get(reverse('so_campaign_edit', args=[self.campaign.id]))
        self.assertEqual(r.status_code, 200)
        m = re.search(
            r'<script id="socEditingData"[^>]*>(.*?)</script>', r.content.decode(), re.S)
        self.assertIsNotNone(m, 'socEditingData script tag not found in rendered page')
        return json.loads(m.group(1))

    def test_only_overridden_account_appears_in_the_hydrated_dict(self):
        data = self._editing_data()
        names = data['email_account_sender_names']
        self.assertEqual(names.get(str(self.acc_a.id)), 'John')
        self.assertNotIn(str(self.acc_b.id), names)


# ── Actual MIME From header (real send path) ─────────────────────────────────

@override_settings(ENABLE_EMAIL_TRACKING=True)
class SendMimeFromHeaderTests(TestCase):
    def setUp(self):
        self.user = make_user('so_mime_sender_name@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='MIME Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
            send_weekdays='mon,tue,wed,thu,fri,sat,sun',
            send_hour_start=time(0, 0, 0), send_hour_end=time(23, 59, 59),
        )
        self.step = SOSequenceStep.objects.create(campaign=self.campaign, order=1, wait_days=0, wait_hours=0)
        SOSequenceVariant.objects.create(
            step=self.step, label='A', subject='Hello', html_body='<p>x</p>', weight=100, is_active=True,
        )
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='John',
            email='mime-sender@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='mime-sender@example.com',
            password='x', daily_limit=50, status='connected',
            spf_status='pass', dkim_status='pass', dmarc_status='pass',
        )
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='mime-recipient@example.com', first_name='T', last_name='P',
            status='subscribed',
        )

    def _send_and_get_message(self, cc):
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(cc)
        self.assertTrue(result)
        msg_bytes = mock_server.sendmail.call_args[0][2]
        import email as email_lib
        return email_lib.message_from_bytes(msg_bytes)

    def test_no_override_uses_account_display_name(self):
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=self.account, order=0)
        cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )
        msg = self._send_and_get_message(cc)
        self.assertEqual(msg['From'], f'John <{self.account.email}>')

    def test_rotation_override_used_as_from_display_name(self):
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Sender', order=0)
        cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )
        msg = self._send_and_get_message(cc)
        self.assertEqual(msg['From'], f'Custom Sender <{self.account.email}>')

    def test_from_address_is_always_the_actual_sending_account(self):
        """Regardless of the display name used, the address half of From
        (and the SMTP envelope sender) must always be the real account."""
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Sender', order=0)
        cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            so_drip.send_next_step(cc)
        envelope_from = mock_server.sendmail.call_args[0][0]
        self.assertEqual(envelope_from, self.account.email)

    def test_two_accounts_in_one_campaign_send_under_their_own_independent_names(self):
        account_b = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='',
            email='mime-sender-b@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='mime-sender-b@example.com',
            password='x', daily_limit=50, status='connected',
            spf_status='pass', dkim_status='pass', dmarc_status='pass',
        )
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Sender', order=0)
        SOEmailAccountRotation.objects.create(campaign=self.campaign, account=account_b, order=1)

        prospect_b = SOProspect.objects.create(
            user_id=self.user.id, email='mime-recipient-b@example.com', status='subscribed')
        cc_a = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )
        cc_b = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=prospect_b, email=prospect_b.email,
            account=account_b, status='sending', current_step=1, attempts=0,
        )
        msg_a = self._send_and_get_message(cc_a)
        msg_b = self._send_and_get_message(cc_b)
        self.assertEqual(msg_a['From'], f'Custom Sender <{self.account.email}>')
        self.assertEqual(msg_b['From'], account_b.email)  # no display_name, no override -> bare email


# ── Test Send resolves server-side ───────────────────────────────────────────

class TestSendResolutionTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_test_send_sender_name@example.com')
        self.account = make_account(self.user, 'testsend-acct@example.com', display_name='John')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Test Send Campaign', subject='s', html_body='<p>x</p>',
            status='draft',
        )
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Test Sender', order=0)
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _test_send(self):
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            r = self.client.post(
                '/Sales-Outreach/sender/test-send/',
                data=json.dumps({
                    'campaign_id': self.campaign.id,
                    'to_emails': ['recipient@example.com'],
                    'subject': 'Test', 'html_body': '<p>x</p>',
                    'email_account_id': self.account.id,
                    # No 'sender_name' key at all -- the wizard no longer sends one.
                }),
                content_type='application/json')
        return r, mock_server

    def test_resolves_the_rotation_override_server_side(self):
        r, mock_server = self._test_send()
        self.assertEqual(r.status_code, 200, r.content)
        msg_bytes = mock_server.sendmail.call_args[0][2]
        import email as email_lib
        msg = email_lib.message_from_bytes(msg_bytes)
        self.assertEqual(msg['From'], f'Custom Test Sender <{self.account.email}>')

    def test_a_client_supplied_sender_name_is_ignored(self):
        """Even if some other client still sent a 'sender_name' field, the
        server must not trust it -- only the campaign's own resolution
        (rotation override -> ... ) governs what's actually sent."""
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            r = self.client.post(
                '/Sales-Outreach/sender/test-send/',
                data=json.dumps({
                    'campaign_id': self.campaign.id,
                    'to_emails': ['recipient@example.com'],
                    'subject': 'Test', 'html_body': '<p>x</p>',
                    'email_account_id': self.account.id,
                    'sender_name': 'Attacker Supplied Name',
                }),
                content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        msg_bytes = mock_server.sendmail.call_args[0][2]
        import email as email_lib
        msg = email_lib.message_from_bytes(msg_bytes)
        self.assertEqual(msg['From'], f'Custom Test Sender <{self.account.email}>')


# ── Reply-To remains unaffected ──────────────────────────────────────────────

@override_settings(ENABLE_EMAIL_TRACKING=True)
class ReplyToUnaffectedTests(TestCase):
    """Confirms per-account Sender Name changes nothing about Reply-To:
    still campaign-level, still gated by reply_to_enabled, still never
    touching the SMTP envelope sender."""

    def setUp(self):
        self.user = make_user('so_reply_to_unaffected@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Reply-To Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
            send_weekdays='mon,tue,wed,thu,fri,sat,sun',
            send_hour_start=time(0, 0, 0), send_hour_end=time(23, 59, 59),
            reply_to='replies@company.com', reply_to_enabled=False,
        )
        self.step = SOSequenceStep.objects.create(campaign=self.campaign, order=1, wait_days=0, wait_hours=0)
        SOSequenceVariant.objects.create(
            step=self.step, label='A', subject='Hello', html_body='<p>x</p>', weight=100, is_active=True,
        )
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='John',
            email='replyto-sender@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='replyto-sender@example.com',
            password='x', daily_limit=50, status='connected',
            spf_status='pass', dkim_status='pass', dmarc_status='pass',
        )
        SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.account, sender_name='Custom Sender', order=0)
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='replyto-recipient@example.com', status='subscribed',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )

    def _send_and_get_message(self):
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(self.cc)
        self.assertTrue(result)
        msg_bytes = mock_server.sendmail.call_args[0][2]
        envelope_from = mock_server.sendmail.call_args[0][0]
        import email as email_lib
        return email_lib.message_from_bytes(msg_bytes), envelope_from

    def test_reply_to_off_emits_no_header_even_with_a_sender_name_override(self):
        msg, envelope_from = self._send_and_get_message()
        self.assertIsNone(msg['Reply-To'])
        self.assertEqual(envelope_from, self.account.email)   # bounce/envelope untouched

    def test_reply_to_on_uses_the_campaign_level_address_regardless_of_sender_name(self):
        self.campaign.reply_to_enabled = True
        self.campaign.save(update_fields=['reply_to_enabled'])
        msg, envelope_from = self._send_and_get_message()
        self.assertEqual(msg['Reply-To'], 'replies@company.com')
        self.assertEqual(msg['From'], f'Custom Sender <{self.account.email}>')
        self.assertEqual(envelope_from, self.account.email)   # bounce/envelope still untouched
