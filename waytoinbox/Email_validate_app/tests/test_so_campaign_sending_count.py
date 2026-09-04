"""Tests for the Sales Outreach "Campaign Sending Count" feature:

  1. SOEmailAccount.daily_limit is now capped at 120 (server + default).
  2. Sender Settings' removed Weight/Percentage system is replaced by a
     per-(campaign, account) Campaign Sending Count (1-120), gated by a new
     campaign-level Enable/Disable toggle (sender_send_count_enabled).

Covers views/so_sender.py::so_campaign_save (via _apply_campaign_payload)
and views/so_email_accounts.py::so_email_account_action's `edit` action.
The lower-level so_drip.py enforcement (per-day quota reservation) and
pick_sender_account() distribution are covered separately in
test_so_drip_send.py::CampaignSendingCountTests/PickSenderAccountTests.
"""
import json

from django.test import TestCase, Client

from Email_validate_app.models import (
    SOCampaign, SOEmailAccount, SOEmailAccountRotation, UserTable,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Sending Count Test', user_email=email, password='StrongPass123!')


def make_account(user, email, daily_limit=120):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=daily_limit, status='connected',
    )


def minimal_sequence():
    return [{'wait_days': 0, 'wait_hours': 0, 'variants': [
        {'label': 'A', 'subject': 'Hello', 'html_body': '<p>x</p>', 'weight': 1},
    ]}]


class CampaignSavePayloadBase(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_save_count@example.com')
        self.acc_a = make_account(self.user, 'save-count-a@example.com', daily_limit=120)
        self.acc_b = make_account(self.user, 'save-count-b@example.com', daily_limit=50)
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _save(self, **overrides):
        payload = {
            'name': 'Sending Count Campaign',
            'action': 'save_draft',
            'sequence': minimal_sequence(),
            'email_account_ids': [self.acc_a.id],
            'email_account_counts': {},
            'sender_send_count_enabled': False,
        }
        payload.update(overrides)
        return self.client.post(
            '/Sales-Outreach/sender/save/', data=json.dumps(payload),
            content_type='application/json')


class ToggleOffTests(CampaignSavePayloadBase):
    def test_toggle_off_stores_the_accounts_own_daily_limit_as_the_row_value(self):
        r = self._save(email_account_ids=[self.acc_a.id])
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sending Count Campaign')
        self.assertFalse(campaign.sender_send_count_enabled)
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        self.assertEqual(rot.daily_send_count, 120)

    def test_toggle_off_ignores_a_submitted_count_entirely(self):
        """Even if the client sends an explicit count while the toggle is
        off, it's not what governs the stored row — the account's own
        daily_limit is."""
        r = self._save(email_account_ids=[self.acc_b.id],
                        email_account_counts={str(self.acc_b.id): 5})
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sending Count Campaign')
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_b)
        self.assertEqual(rot.daily_send_count, 50)   # acc_b's own daily_limit, not 5


class ToggleOnTests(CampaignSavePayloadBase):
    def test_valid_count_within_range_is_stored_and_toggle_persisted(self):
        r = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 30},
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sending Count Campaign')
        self.assertTrue(campaign.sender_send_count_enabled)
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        self.assertEqual(rot.daily_send_count, 30)

    def test_two_accounts_each_keep_their_own_independent_count(self):
        r = self._save(
            email_account_ids=[self.acc_a.id, self.acc_b.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 30, str(self.acc_b.id): 34},
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sending Count Campaign')
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a).daily_send_count, 30)
        self.assertEqual(
            SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_b).daily_send_count, 34)

    def test_explicit_count_above_120_is_rejected(self):
        r = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 200},
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('account', r.json()['errors'])
        self.assertFalse(SOCampaign.objects.filter(name='Sending Count Campaign').exists())

    def test_explicit_count_above_the_accounts_own_lower_daily_limit_is_rejected(self):
        """acc_b's own daily_limit is 50 -- even though 60 <= 120, it must
        still be rejected: a campaign can never ask an account to send more
        than the account itself is configured for."""
        r = self._save(
            email_account_ids=[self.acc_b.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_b.id): 60},
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('account', r.json()['errors'])

    def test_explicit_zero_is_rejected(self):
        r = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 0},
        )
        self.assertEqual(r.status_code, 400, r.content)

    def test_missing_count_on_a_draft_save_falls_back_silently_instead_of_erroring(self):
        """An in-progress draft must never fail to save just because the
        user hasn't filled in a count yet."""
        r = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={}, action='save_draft',
        )
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Sending Count Campaign')
        rot = SOEmailAccountRotation.objects.get(campaign=campaign, account=self.acc_a)
        self.assertEqual(rot.daily_send_count, 120)   # fell back to the account's own ceiling

    def test_editing_an_existing_campaign_updates_its_stored_count(self):
        r = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 30},
        )
        campaign_id = r.json()['campaign_id']

        r2 = self._save(
            email_account_ids=[self.acc_a.id], sender_send_count_enabled=True,
            email_account_counts={str(self.acc_a.id): 45}, campaign_id=campaign_id,
        )
        self.assertEqual(r2.status_code, 200, r2.content)
        rot = SOEmailAccountRotation.objects.get(campaign_id=campaign_id, account=self.acc_a)
        self.assertEqual(rot.daily_send_count, 45)


class DailyLimitMaxTests(TestCase):
    """SOEmailAccount.daily_limit is now capped at 120."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('so_daily_limit_max@example.com')
        self.account = make_account(self.user, 'daily-limit-max@example.com', daily_limit=50)
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _edit(self, daily_limit):
        return self.client.post(
            '/Sales-Outreach/so-accounts/action/',
            data=json.dumps({'action': 'edit', 'id': self.account.id, 'daily_limit': daily_limit}),
            content_type='application/json')

    def test_new_account_defaults_to_120(self):
        acc = make_account(self.user, 'fresh-default@example.com')
        self.assertEqual(acc.daily_limit, 120)

    def test_121_is_rejected(self):
        r = self._edit(121)
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('daily_limit', r.json()['errors'])
        self.account.refresh_from_db()
        self.assertEqual(self.account.daily_limit, 50)   # unchanged

    def test_120_is_accepted(self):
        r = self._edit(120)
        self.assertEqual(r.json()['status'], 'ok')
        self.account.refresh_from_db()
        self.assertEqual(self.account.daily_limit, 120)

    def test_zero_is_still_rejected(self):
        r = self._edit(0)
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('daily_limit', r.json()['errors'])
