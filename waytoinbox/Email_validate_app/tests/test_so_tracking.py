"""Tests for Sales Outreach open/click tracking
(views/so_tracking.py, services/so_smtp.py::inject_tracking).

Tracking is force-enabled per-call via inject_tracking(enable_tracking=True)
rather than depending on settings.ENABLE_EMAIL_TRACKING, so these tests are
independent of the DJANGO_ENV-derived local-dev safety gate (see that
setting's own comment in Innovicloud/settings.py) — they verify the
tracking MECHANISM itself, which is what a campaign send exercises once
that gate is actually on (production, or ENABLE_EMAIL_TRACKING=1 locally).
"""
from django.test import TestCase, override_settings
from django.urls import reverse

from Email_validate_app.models import (
    UserTable, SOCampaign, SOCampaignContact, SOProspect, SOEvent,
    SOTrackedLink, SOOpenPixel,
)
from Email_validate_app.services.so_smtp import inject_tracking


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Tracking Test', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class _TrackingTestCase(TestCase):
    def setUp(self):
        self.user = make_user('so_tracking@example.com')
        self.campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Test Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
        )
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='tracking-test-prospect@example.com',
            first_name='Test', last_name='Prospect',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            status='sending', current_step=1,
        )


class InjectTrackingTests(_TrackingTestCase):
    """5. Email HTML contains the open pixel. 6. Email HTML contains
    rewritten click URLs. 7. Tracking ID maps to the correct campaign/
    message/recipient."""

    def test_email_html_contains_rewritten_click_url_and_open_pixel(self):
        raw_html = '<html><body><p><a href="https://example.com/offer">Offer</a></p></body></html>'
        html, tracked_links, open_pixel = inject_tracking(
            raw_html, self.cc, enable_tracking=True, step_order=1,
        )

        self.assertEqual(len(tracked_links), 1)
        self.assertIsNotNone(open_pixel)
        self.assertIn(f'/so/track/click/{tracked_links[0].token}/', html)
        self.assertIn(f'/so/track/pixel/{open_pixel.token}/', html)
        self.assertIn('<img src=', html)

        # Tracking ID maps to the correct campaign/contact/prospect.
        self.assertEqual(tracked_links[0].campaign_contact_id, self.cc.id)
        self.assertEqual(tracked_links[0].destination_url, 'https://example.com/offer')
        self.assertEqual(open_pixel.campaign_contact_id, self.cc.id)

    def test_disabled_tracking_leaves_html_and_links_untouched(self):
        raw_html = '<html><body><p><a href="https://example.com/offer">Offer</a></p></body></html>'
        html, tracked_links, open_pixel = inject_tracking(
            raw_html, self.cc, enable_tracking=False, step_order=1,
        )
        self.assertEqual(html, raw_html)
        self.assertEqual(tracked_links, [])
        self.assertIsNone(open_pixel)
        self.assertNotIn('/so/track/', html)


class OpenTrackingEndpointTests(_TrackingTestCase):
    """1. Open tracking endpoint creates SalesTrackingEvent (SOEvent in this
    codebase). 2. Open endpoint returns the expected GIF response."""

    def setUp(self):
        super().setUp()
        self.pixel = SOOpenPixel.objects.create(campaign_contact=self.cc, step_order=1)

    def test_pixel_hit_creates_opened_event_and_returns_gif(self):
        url = reverse('so_track_pixel', args=[self.pixel.token])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/gif')
        self.assertTrue(response.content.startswith(b'GIF89a'))

        event = SOEvent.objects.get(campaign=self.campaign, event_type='opened')
        self.assertEqual(event.email, self.cc.email)
        self.assertEqual(event.step_order, 1)

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 1)

    def test_legacy_open_endpoint_also_creates_opened_event(self):
        """so_track_open (the older, per-contact-token pixel) must keep
        working unchanged for every already-sent email using it."""
        url = reverse('so_track_open', args=[self.cc.tracking_token])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/gif')
        event = SOEvent.objects.get(campaign=self.campaign, event_type='opened')
        self.assertEqual(event.email, self.cc.email)

    def test_unknown_pixel_token_still_returns_a_valid_gif(self):
        """An unresolvable token must not surface an error to the recipient
        — the pixel request itself always succeeds, it simply records no
        event."""
        import uuid
        response = self.client.get(reverse('so_track_pixel', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/gif')
        self.assertFalse(SOEvent.objects.filter(campaign=self.campaign, event_type='opened').exists())


class ClickTrackingEndpointTests(_TrackingTestCase):
    """3. Click tracking endpoint creates SalesTrackingEvent (SOEvent).
    4. Click endpoint redirects to the original destination URL."""

    def setUp(self):
        super().setUp()
        self.link = SOTrackedLink.objects.create(
            campaign_contact=self.cc, destination_url='https://example.com/offer', step_order=1,
        )

    def test_click_hit_creates_clicked_event_and_redirects(self):
        url = reverse('so_track_click', args=[self.link.token])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://example.com/offer')

        event = SOEvent.objects.get(campaign=self.campaign, event_type='clicked')
        self.assertEqual(event.email, self.cc.email)
        self.assertEqual(event.metadata.get('url'), 'https://example.com/offer')
        self.assertEqual(event.step_order, 1)

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_clicked, 1)

    def test_unknown_click_token_redirects_to_site_url_without_error(self):
        import uuid
        from Email_validate_app.services.so_smtp import SITE_URL
        response = self.client.get(reverse('so_track_click', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, SITE_URL)
        self.assertFalse(SOEvent.objects.filter(campaign=self.campaign, event_type='clicked').exists())


class TrackingAnalyticsTests(_TrackingTestCase):
    """8. Analytics returns the recorded open/click events — confirms
    so_analytics.py reads the exact event_type values the tracking views
    write, not a stale/parallel metric."""

    def test_recorded_events_are_visible_via_the_campaign_relation(self):
        SOEvent.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.cc.email,
            event_type='opened', metadata={},
        )
        SOEvent.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.cc.email,
            event_type='clicked', metadata={'url': 'https://example.com/offer'},
        )

        opened = self.campaign.events.filter(event_type='opened')
        clicked = self.campaign.events.filter(event_type='clicked')
        self.assertEqual(opened.count(), 1)
        self.assertEqual(clicked.count(), 1)
        self.assertEqual(opened.first().email, self.cc.email)
        self.assertEqual(clicked.first().metadata.get('url'), 'https://example.com/offer')
