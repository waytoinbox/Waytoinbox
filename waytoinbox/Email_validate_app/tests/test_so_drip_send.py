"""Focused tests for Phase 2 of the tracking fix.

Covers:
  1. Full inject_tracking() -> save -> pixel request -> SOEvent(opened).
  2. Full click-link creation -> save -> click request -> SOEvent(clicked).
  3. send_next_step() normal SMTP-success path: tracking persists, contact
     advances via _record_success (the pre-existing, correct behavior).
  4. THE FIX: send_next_step() when SMTP succeeds but the post-send
     tracking persistence (SOTrackedLink.bulk_create / open_pixel.save)
     raises — must NOT call _record_failure, must NOT release the quota
     slot, and must still advance the contact through _record_success
     exactly as a normal successful send would.
  5. Regression guard: a genuine SMTP failure (before any tracking
     persistence is attempted) must still follow the existing failure/
     retry path unchanged — quota released, attempts incremented,
     status back to 'active'.

All tests create and delete their own isolated fixtures.
"""
from datetime import time
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from datetime import timedelta

from Email_validate_app.models import (
    UserTable, SOCampaign, SOCampaignContact, SOProspect, SOEmailAccount,
    SOEmailAccountDailyUsage, SOCampaignAccountDailyUsage, SOEmailAccountRotation,
    SOEvent, SOOpenPixel, SOTrackedLink, SOSequenceStep, SOSequenceVariant,
)
from Email_validate_app.services.so_smtp import inject_tracking
from Email_validate_app.services import so_drip
from Email_validate_app.services.trial_manager import activate_trial


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Drip Test', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class OpenAndClickPersistenceTests(TestCase):
    """Items 1 and 2 of the Phase-2 validation list."""

    def setUp(self):
        self.user = make_user('drip_open_click@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Test Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
        )
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='drip-test-prospect@example.com', first_name='T', last_name='P',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            status='sending', current_step=1,
        )
        self.client = self.client_class(SERVER_NAME='127.0.0.1')

    def test_inject_tracking_then_save_then_pixel_request_creates_opened_event(self):
        html, tracked_links, open_pixel = inject_tracking(
            '<html><body><p><a href="https://example.com/x">go</a></p></body></html>',
            self.cc, enable_tracking=True, step_order=1,
        )
        open_pixel.save()

        response = self.client.get(reverse('so_track_pixel', args=[open_pixel.token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/gif')

        event = SOEvent.objects.get(campaign=self.campaign, event_type='opened')
        self.assertEqual(event.email, self.cc.email)
        self.assertEqual(event.step_order, 1)

    def test_inject_tracking_then_save_then_click_request_creates_clicked_event(self):
        html, tracked_links, open_pixel = inject_tracking(
            '<html><body><p><a href="https://example.com/offer">go</a></p></body></html>',
            self.cc, enable_tracking=True, step_order=1,
        )
        SOTrackedLink.objects.bulk_create(tracked_links, ignore_conflicts=True)
        link = tracked_links[0]

        response = self.client.get(reverse('so_track_click', args=[link.token]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://example.com/offer')

        event = SOEvent.objects.get(campaign=self.campaign, event_type='clicked')
        self.assertEqual(event.email, self.cc.email)
        self.assertEqual(event.metadata.get('url'), 'https://example.com/offer')


class SendNextStepTests(TestCase):
    """Items 3, 4, 5 — the actual send_next_step() fix verification."""

    def setUp(self):
        self.user = make_user('drip_send_next@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Test Send Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
            send_weekdays='mon,tue,wed,thu,fri,sat,sun',
            send_hour_start=time(0, 0, 0), send_hour_end=time(23, 59, 59),
        )
        self.step = SOSequenceStep.objects.create(campaign=self.campaign, order=1, wait_days=0, wait_hours=0)
        self.variant = SOSequenceVariant.objects.create(
            step=self.step, label='A', subject='Hello', html_body='<p><a href="https://example.com/x">go</a></p>',
            weight=100, is_active=True,
        )
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Test Sender',
            email='drip-test-sender@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='drip-test-sender@example.com',
            password='x', daily_limit=50, status='connected',
        )
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='drip-test-recipient@example.com', first_name='T', last_name='P',
            status='subscribed',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )

    def _quota_used(self):
        row = SOEmailAccountDailyUsage.objects.filter(account=self.account).first()
        return row.sent_count if row else 0

    @override_settings(ENABLE_EMAIL_TRACKING=True)
    def test_normal_smtp_success_persists_tracking_and_records_success(self):
        """Item 3 — baseline: everything succeeds, contact advances normally."""
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}

        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(self.cc)

        self.assertTrue(result)
        self.cc.refresh_from_db()
        self.assertEqual(self.cc.status, 'completed')  # only step -> completed
        self.assertEqual(self.cc.current_step, 2)
        self.assertEqual(self._quota_used(), 1)
        self.assertTrue(SOOpenPixel.objects.filter(campaign_contact=self.cc).exists())
        self.assertTrue(SOTrackedLink.objects.filter(campaign_contact=self.cc).exists())
        self.assertTrue(SOEvent.objects.filter(campaign=self.campaign, event_type='sent').exists())

    @override_settings(ENABLE_EMAIL_TRACKING=True)
    def test_post_send_persistence_failure_does_not_trigger_retry_or_release_quota(self):
        """Item 4 — THE FIX. SMTP succeeds, then open_pixel.save() raises.
        Must NOT go through _record_failure: no retry, no quota release,
        and the contact must still advance via _record_success (the email
        was genuinely delivered)."""
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}

        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server), \
             patch('Email_validate_app.models.SOOpenPixel.save', side_effect=Exception('simulated DB failure')), \
             patch(f'{so_drip.__name__}.logger') as mock_logger:
            result = so_drip.send_next_step(self.cc)

        # Must still report success — the email really was delivered.
        self.assertTrue(result)

        self.cc.refresh_from_db()
        # Must NOT be back in 'active' with attempts incremented (the
        # retry/failure path) — must have advanced normally instead.
        self.assertEqual(self.cc.status, 'completed')
        self.assertEqual(self.cc.current_step, 2)
        self.assertEqual(self.cc.attempts, 0)

        # Quota must remain consumed — a real send happened.
        self.assertEqual(self._quota_used(), 1)

        # The 'sent'/'delivered' events from _record_success must exist —
        # proof _record_success actually ran, not _record_failure.
        self.assertTrue(SOEvent.objects.filter(campaign=self.campaign, event_type='sent').exists())
        self.assertTrue(SOEvent.objects.filter(campaign=self.campaign, event_type='delivered').exists())

        # The failure must have been logged loudly.
        mock_logger.exception.assert_called_once()

    @override_settings(ENABLE_EMAIL_TRACKING=True)
    def test_genuine_smtp_failure_still_follows_existing_retry_path(self):
        """Item 5 — regression guard. A real SMTP failure (before sendmail
        ever succeeds) must be completely unaffected by the fix: quota
        released, attempts incremented, status back to 'active'."""
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = Exception('simulated SMTP failure')

        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(self.cc)

        self.assertFalse(result)

        self.cc.refresh_from_db()
        self.assertEqual(self.cc.status, 'active')       # existing failure path
        self.assertEqual(self.cc.current_step, 1)         # never advanced
        self.assertEqual(self.cc.attempts, 1)
        self.assertIsNotNone(self.cc.next_action_at)

        # Quota must be released — no send actually happened.
        self.assertEqual(self._quota_used(), 0)

        # No tracking rows, no sent/delivered events.
        self.assertFalse(SOOpenPixel.objects.filter(campaign_contact=self.cc).exists())
        self.assertFalse(SOEvent.objects.filter(campaign=self.campaign, event_type='sent').exists())


class TrialDoesNotNarrowSalesOutreachQuotaTests(TestCase):
    """so_drip._reserve_quota_slot() is NOT narrowed by an active free
    trial -- Sales Outreach's real per-account send volume always uses the
    account's own configured daily_limit, trial or no trial (the previous
    min(daily_limit, 7) override was removed; see trial_manager.py's own
    docstring note on this)."""

    def _make_account(self, daily_limit):
        self.user = make_user(f'so_trial_no_cap_{daily_limit}@example.com')
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Trial Sender',
            email=f'trial-sender-{daily_limit}@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username=f'trial-sender-{daily_limit}@example.com',
            password='x', daily_limit=daily_limit, status='connected',
        )
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name=f'Trial No-Cap Campaign {daily_limit}', subject='s', html_body='<p>x</p>',
            status='sending',
        )
        return self.account

    def test_reserve_quota_slot_return_signature(self):
        account = self._make_account(daily_limit=50)
        result = so_drip._reserve_quota_slot(self.campaign, account)
        self.assertIsInstance(result, tuple)
        claimed, effective_limit = result
        self.assertTrue(claimed)
        self.assertEqual(effective_limit, 50)

    def test_active_trial_does_not_narrow_the_daily_limit(self):
        account = self._make_account(daily_limit=50)
        activate_trial(self.user)

        for i in range(50):
            claimed, effective_limit = so_drip._reserve_quota_slot(self.campaign, account)
            self.assertTrue(claimed, f"send {i + 1} should have been claimed")
            self.assertEqual(effective_limit, 50, "an active trial must not cap this below the account's own daily_limit")

        claimed, effective_limit = so_drip._reserve_quota_slot(self.campaign, account)
        self.assertFalse(claimed, "the 51st send must be refused by the account's own daily_limit, not a trial cap")
        self.assertEqual(effective_limit, 50)


class CampaignSendingCountTests(TestCase):
    """Campaign Sending Count — replaces the removed Weight/Percentage
    system. Covers the new per-(campaign, account, day) cap
    _reserve_quota_slot/_release_quota_slot enforce only while
    campaign.sender_send_count_enabled is True, and pick_sender_account's
    distribution."""

    def setUp(self):
        self.user = make_user('so_send_count@example.com')
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Count Sender',
            email='count-sender@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='count-sender@example.com',
            password='x', daily_limit=50, status='connected',
        )

    def _campaign(self, enabled):
        return SOCampaign.objects.create(
            user_id=self.user.id, name=f'Count Campaign {enabled}', subject='s', html_body='<p>x</p>',
            status='sending', sender_send_count_enabled=enabled,
        )

    def test_toggle_off_ignores_daily_send_count_uses_account_limit_only(self):
        campaign = self._campaign(enabled=False)
        SOEmailAccountRotation.objects.create(campaign=campaign, account=self.account, daily_send_count=2)

        for i in range(50):
            claimed, effective_limit = so_drip._reserve_quota_slot(campaign, self.account)
            self.assertTrue(claimed, f"send {i + 1} should have been claimed")
            self.assertEqual(effective_limit, 50)

        claimed, _ = so_drip._reserve_quota_slot(campaign, self.account)
        self.assertFalse(claimed, "the 51st send must be refused by the account's own daily_limit")
        self.assertEqual(SOCampaignAccountDailyUsage.objects.count(), 0,
                          "toggle off must never write a per-campaign usage row")

    def test_toggle_on_caps_below_the_accounts_own_daily_limit(self):
        campaign = self._campaign(enabled=True)
        SOEmailAccountRotation.objects.create(campaign=campaign, account=self.account, daily_send_count=2)

        for i in range(2):
            claimed, effective_limit = so_drip._reserve_quota_slot(campaign, self.account)
            self.assertTrue(claimed, f"send {i + 1} should have been claimed")
            # A successful claim always reports the account-level cap (same
            # contract as every other passing case in this file) — it's
            # only on a REFUSED claim that effective_limit names whichever
            # cap actually blocked it, checked just below.
            self.assertEqual(effective_limit, 50)

        claimed, effective_limit = so_drip._reserve_quota_slot(campaign, self.account)
        self.assertFalse(claimed, "the 3rd send must be refused by this campaign's own count, "
                                   "well below the account's daily_limit=50")
        self.assertEqual(effective_limit, 2)
        # The account-level slot claimed then immediately given back (since
        # the campaign-level cap blocked it) must not leak — only the 2
        # successful sends should be reflected in the account-global usage.
        usage = SOEmailAccountDailyUsage.objects.get(account=self.account)
        self.assertEqual(usage.sent_count, 2)

    def test_two_campaigns_sharing_one_account_have_independent_caps(self):
        campaign_a = self._campaign(enabled=True)
        campaign_b = self._campaign(enabled=True)
        SOEmailAccountRotation.objects.create(campaign=campaign_a, account=self.account, daily_send_count=2)
        SOEmailAccountRotation.objects.create(campaign=campaign_b, account=self.account, daily_send_count=3)

        for _ in range(2):
            self.assertTrue(so_drip._reserve_quota_slot(campaign_a, self.account)[0])
        self.assertFalse(so_drip._reserve_quota_slot(campaign_a, self.account)[0],
                          "campaign A must be capped at its own count of 2")

        # Campaign B's own cap (3) is untouched by campaign A being exhausted.
        for i in range(3):
            self.assertTrue(so_drip._reserve_quota_slot(campaign_b, self.account)[0],
                             f"campaign B send {i + 1} should have been claimed")
        self.assertFalse(so_drip._reserve_quota_slot(campaign_b, self.account)[0])

        # But the account-global cap (50) still governs both combined.
        usage = SOEmailAccountDailyUsage.objects.get(account=self.account)
        self.assertEqual(usage.sent_count, 5)   # 2 (A) + 3 (B)

    def test_toggle_on_shared_account_cap_still_blocks_across_campaigns(self):
        """The per-campaign cap only ever narrows the account's own
        daily_limit, never widens it -- two campaigns with generous
        per-campaign counts still can't collectively exceed the account's
        real daily_limit."""
        self.account.daily_limit = 3
        self.account.save(update_fields=['daily_limit'])
        campaign_a = self._campaign(enabled=True)
        campaign_b = self._campaign(enabled=True)
        SOEmailAccountRotation.objects.create(campaign=campaign_a, account=self.account, daily_send_count=3)
        SOEmailAccountRotation.objects.create(campaign=campaign_b, account=self.account, daily_send_count=3)

        self.assertTrue(so_drip._reserve_quota_slot(campaign_a, self.account)[0])
        self.assertTrue(so_drip._reserve_quota_slot(campaign_a, self.account)[0])
        self.assertTrue(so_drip._reserve_quota_slot(campaign_b, self.account)[0])
        # Account-wide daily_limit=3 is now exhausted, even though campaign
        # B's own per-campaign count (3) has only been used once.
        self.assertFalse(so_drip._reserve_quota_slot(campaign_b, self.account)[0])

    def test_release_quota_slot_gives_back_both_counters_when_enabled(self):
        campaign = self._campaign(enabled=True)
        SOEmailAccountRotation.objects.create(campaign=campaign, account=self.account, daily_send_count=5)

        self.assertTrue(so_drip._reserve_quota_slot(campaign, self.account)[0])
        so_drip._release_quota_slot(campaign, self.account)

        self.assertEqual(SOEmailAccountDailyUsage.objects.get(account=self.account).sent_count, 0)
        self.assertEqual(
            SOCampaignAccountDailyUsage.objects.get(campaign=campaign, account=self.account).sent_count, 0)

    def test_release_quota_slot_only_touches_account_level_when_disabled(self):
        campaign = self._campaign(enabled=False)
        self.assertTrue(so_drip._reserve_quota_slot(campaign, self.account)[0])
        so_drip._release_quota_slot(campaign, self.account)

        self.assertEqual(SOEmailAccountDailyUsage.objects.get(account=self.account).sent_count, 0)
        self.assertFalse(SOCampaignAccountDailyUsage.objects.filter(campaign=campaign).exists())

    def test_missing_rotation_row_falls_back_to_account_limit_as_the_cap(self):
        """Defensive: a campaign with the toggle on but no rotation row for
        this particular account (shouldn't normally happen) must not crash
        -- it falls back to the account's own daily_limit as the cap."""
        campaign = self._campaign(enabled=True)
        claimed, effective_limit = so_drip._reserve_quota_slot(campaign, self.account)
        self.assertTrue(claimed)
        self.assertEqual(effective_limit, 50)


class PickSenderAccountTests(TestCase):
    """pick_sender_account() — replaces the removed pick_weighted_account.
    Deterministic per (campaign_id, email); weighted by daily_send_count
    only when send_count_enabled is True, uniform otherwise."""

    def setUp(self):
        self.user = make_user('so_pick_sender@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Pick Sender Campaign', subject='s', html_body='<p>x</p>',
            status='draft',
        )
        self.acc_a = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='A',
            email='pick-a@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='pick-a@example.com',
            password='x', daily_limit=50, status='connected',
        )
        self.acc_b = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='B',
            email='pick-b@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='pick-b@example.com',
            password='x', daily_limit=50, status='connected',
        )

    def test_empty_rotations_returns_none(self):
        self.assertIsNone(so_drip.pick_sender_account(self.campaign.id, 'x@example.com', [], True))

    def test_single_rotation_always_returned(self):
        rot = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_a, daily_send_count=1)
        picked = so_drip.pick_sender_account(self.campaign.id, 'x@example.com', [rot], True)
        self.assertEqual(picked.id, self.acc_a.id)

    def test_deterministic_for_the_same_inputs(self):
        rot_a = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_a, daily_send_count=30)
        rot_b = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_b, daily_send_count=90)
        first = so_drip.pick_sender_account(self.campaign.id, 'sticky@example.com', [rot_a, rot_b], True)
        second = so_drip.pick_sender_account(self.campaign.id, 'sticky@example.com', [rot_a, rot_b], True)
        self.assertEqual(first.id, second.id)

    def test_disabled_ignores_daily_send_count_skew(self):
        """With the toggle off, a wildly unequal daily_send_count between
        two accounts must not skew the distribution -- every eligible
        rotation is drawn uniformly."""
        rot_a = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_a, daily_send_count=1)
        rot_b = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_b, daily_send_count=119)
        picks = [
            so_drip.pick_sender_account(self.campaign.id, f'user{i}@example.com', [rot_a, rot_b], False).id
            for i in range(60)
        ]
        count_a = picks.count(self.acc_a.id)
        count_b = picks.count(self.acc_b.id)
        # Both must get a meaningful share — a 119:1 weighted draw would
        # make count_a implausibly close to 0.
        self.assertGreater(count_a, 15)
        self.assertGreater(count_b, 15)

    def test_enabled_skews_distribution_toward_the_higher_count(self):
        rot_a = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_a, daily_send_count=5)
        rot_b = SOEmailAccountRotation.objects.create(
            campaign=self.campaign, account=self.acc_b, daily_send_count=115)
        picks = [
            so_drip.pick_sender_account(self.campaign.id, f'user{i}@example.com', [rot_a, rot_b], True).id
            for i in range(60)
        ]
        count_a = picks.count(self.acc_a.id)
        count_b = picks.count(self.acc_b.id)
        self.assertGreater(count_b, count_a)
