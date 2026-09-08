"""Sales Outreach New Campaign -> Settings -> Reply-To Address toggle.

The Reply-To Address field was previously stored and used unconditionally
whenever non-blank. This adds SOCampaign.reply_to_enabled (default False)
so the stored address is only actually used once its toggle is turned on
-- off means today's pre-toggle default behavior (no Reply-To header on
outgoing mail, replies matched only against the sending account).

Covers views/so_sender.py::so_campaign_save (via _apply_campaign_payload)
and _duplicate_campaign's own copy of the field. Outgoing header behavior
(services/so_drip.py) and incoming reply-mailbox matching
(services/so_imap.py::_mailbox_is_valid_for_reply) are covered separately
in test_so_imap_sync.py (ReplyToHeaderTests / ReplyToMailboxTrackingTests).
"""
import json

from django.test import TestCase, Client

from Email_validate_app.models import SOCampaign, SOEmailAccount, UserTable


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Reply-To Toggle Test', user_email=email, password='StrongPass123!')


def make_account(user, email):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=50, status='connected',
    )


def minimal_sequence():
    return [{'wait_days': 0, 'wait_hours': 0, 'variants': [
        {'label': 'A', 'subject': 'Hello', 'html_body': '<p>x</p>', 'weight': 1},
    ]}]


class ReplyToTogglePayloadTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('reply_to_toggle_save@example.com')
        self.account = make_account(self.user, 'sender@example.com')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _save(self, **overrides):
        payload = {
            'name': 'Reply-To Toggle Campaign',
            'action': 'save_draft',
            'sequence': minimal_sequence(),
            'email_account_ids': [self.account.id],
            'email_account_counts': {},
        }
        payload.update(overrides)
        return self.client.post(
            '/Sales-Outreach/sender/save/', data=json.dumps(payload),
            content_type='application/json')

    def test_toggle_off_by_default_when_omitted(self):
        """A payload that predates this field (or a client that simply
        never sent the key) must default to off, matching the model
        field's own default -- never a silent opt-in."""
        r = self._save(reply_to='sales@company.com')
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Reply-To Toggle Campaign')
        self.assertEqual(campaign.reply_to, 'sales@company.com')
        self.assertFalse(campaign.reply_to_enabled)

    def test_toggle_on_is_persisted_alongside_the_address(self):
        r = self._save(reply_to='sales@company.com', reply_to_enabled=True)
        self.assertEqual(r.status_code, 200, r.content)
        campaign = SOCampaign.objects.get(name='Reply-To Toggle Campaign')
        self.assertEqual(campaign.reply_to, 'sales@company.com')
        self.assertTrue(campaign.reply_to_enabled)

    def test_address_survives_toggling_off_then_back_on(self):
        """Turning the toggle off must not clear/lose the typed address --
        only stop it from being used -- so re-enabling later doesn't
        require retyping it."""
        r1 = self._save(reply_to='sales@company.com', reply_to_enabled=True)
        self.assertEqual(r1.status_code, 200, r1.content)
        campaign_id = SOCampaign.objects.get(name='Reply-To Toggle Campaign').id

        r2 = self._save(campaign_id=campaign_id, reply_to='sales@company.com', reply_to_enabled=False)
        self.assertEqual(r2.status_code, 200, r2.content)
        campaign = SOCampaign.objects.get(id=campaign_id)
        self.assertEqual(campaign.reply_to, 'sales@company.com')
        self.assertFalse(campaign.reply_to_enabled)

    def test_loading_the_campaign_back_into_the_wizard_reflects_the_toggle(self):
        """views/so_sender.py's `editing` context (used to hydrate the
        wizard when re-opening a draft, embedded as the socEditingData
        json_script) must echo reply_to_enabled, not just reply_to itself."""
        import re

        r = self._save(reply_to='sales@company.com', reply_to_enabled=True)
        self.assertEqual(r.status_code, 200, r.content)
        campaign_id = SOCampaign.objects.get(name='Reply-To Toggle Campaign').id

        page = self.client.get(f'/Sales-Outreach/sender/{campaign_id}/edit/')
        self.assertEqual(page.status_code, 200)
        match = re.search(
            r'<script id="socEditingData"[^>]*>(.*?)</script>', page.content.decode(), re.DOTALL,
        )
        self.assertIsNotNone(match, 'socEditingData json_script tag not found on the edit page')
        editing = json.loads(match.group(1))
        self.assertEqual(editing['reply_to'], 'sales@company.com')
        self.assertTrue(editing['reply_to_enabled'])


class DuplicateCampaignReplyToTests(TestCase):
    def setUp(self):
        self.user = make_user('reply_to_toggle_dup@example.com')

    def test_duplicate_copies_both_the_address_and_the_toggle(self):
        from Email_validate_app.models import SOCampaign as SOCampaignModel
        from Email_validate_app.views.so_sender import _duplicate_campaign

        campaign = SOCampaignModel.objects.create(
            user_id=self.user.id, name='Original', subject='s', html_body='<p>x</p>',
            status='draft', reply_to='sales@company.com', reply_to_enabled=True,
        )
        dup = _duplicate_campaign(campaign, self.user.id)
        self.assertEqual(dup.reply_to, 'sales@company.com')
        self.assertTrue(dup.reply_to_enabled)
