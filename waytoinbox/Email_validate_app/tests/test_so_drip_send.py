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

from Email_validate_app.models import (
    UserTable, SOCampaign, SOCampaignContact, SOProspect, SOEmailAccount,
    SOEmailAccountDailyUsage, SOEvent, SOOpenPixel, SOTrackedLink,
    SOSequenceStep, SOSequenceVariant,
)
from Email_validate_app.services.so_smtp import inject_tracking
from Email_validate_app.services import so_drip


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
