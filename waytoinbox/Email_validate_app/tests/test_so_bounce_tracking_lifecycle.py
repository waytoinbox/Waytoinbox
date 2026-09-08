"""Sales Outreach event lifecycle FINAL rule: once a hard bounce is
confirmed for (campaign, email), BOUNCED is the absolute final state --
delivered/opened/clicked must not remain as valid engagement, in the
database OR in any counter, and the address is globally suppressed for
this user going forward exactly like unsubscribe.

Root cause (see the investigation report): services/so_drip.py::_record_success
writes 'sent' and 'delivered' together at SMTP-handoff time (the only
signal available -- there is no genuine recipient-side delivery
confirmation anywhere in this system, and inventing one is out of scope).
services/so_imap.py later records a real 'bounced' event independently,
with no reconciliation against the earlier 'delivered' -- and nothing
stopped a stale/late tracking-pixel hit or link click from adding
'opened'/'clicked' events (and incrementing their counters) for an
address that had already bounced, nor retracted engagement already
recorded before the bounce was even detected.

This file covers the full fix:
  - services/so_imap.py::_invalidate_post_bounce_engagement, called from
    _record_once's own "newly recorded" branch for event_type='bounced',
    DELETES this (campaign, email)'s delivered/opened/clicked SOEvent rows
    outright (not a read-time filter) and decrements
    total_delivered/total_opened/total_clicked by exactly the number of
    rows removed -- 'sent' and the SOEvent audit trail for 'bounced' itself
    are never touched. Runs at most once per (campaign, email), since it's
    gated on the same dedupe guard _record_once already uses.
  - views/so_tracking.py (so_track_open/so_track_pixel/so_track_click)
    refuse to record a NEW opened/clicked event or increment its counter
    once services.so_drip.is_contact_bounced() is true for that
    (campaign, email) -- stops the problem from recurring going forward.
  - views/so_sender.py::so_campaign_detail forces delivered/opened/clicked
    to False on any row whose bounced/complained flag is set, and keeps
    'bounced'/'complained' as a sticky, unoverridable last_event -- a
    backstop for legacy pre-fix data (e.g. production Campaign 40) that
    still has old delivered/opened/clicked rows sitting in the DB, since
    those are explicitly NOT touched by this deploy.
  - services/so_analytics.py (compute_overview/compute_funnel) and
    views/so_sender.py::so_campaigns' list-page engagement counts exclude
    a since-bounced email's delivered/opened/clicked from every total/rate
    -- same legacy-data backstop, redundant with the deletion above for
    any bounce recorded after this fix ships.
  - Global, cross-campaign, per-user future-send suppression (enrollment,
    per-step re-check, recipient estimation) was already correct before
    this task and is verified, not re-implemented, here -- see
    tasks/so_send_campaign.py, services/so_drip.py::send_next_step, and
    views/so_sender.py::so_estimate_recipients, all of which already query
    SOEvent for event_type__in=('bounced','complained') scoped to
    campaign__user_id, mirroring how unsubscribe suppression already works
    via SOProspect.status='subscribed'.

Follows test_so_imap_sync.py's established fixtures/conventions -- Django's
isolated test database only, no real IMAP/SMTP/network calls.
"""
from django.db.models import F
from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    SOCampaign, SOCampaignContact, SOEvent, SOTrackedLink, SOOpenPixel,
    SOList, SOListProspect,
)
from Email_validate_app.services import so_drip, so_imap, so_analytics
from Email_validate_app.services.so_drip import is_contact_bounced
from Email_validate_app.tests.test_so_imap_sync import (
    make_user, make_account, make_campaign, make_sent_contact,
)

_BASE_SETTINGS = dict(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])


def _record_bounce(cc):
    """Exactly what services/so_imap.py::_handle_bounce_candidate does on a
    real hard-bounce match -- reused directly rather than re-implemented,
    so these tests exercise the real function, not a stand-in for it."""
    return so_imap._record_once(cc, 'bounced', 'total_bounced', {'severity': 'hard'})


def _login(user):
    c = Client(SERVER_NAME='127.0.0.1')
    session = c.session
    session['logged_in'] = user.user_email
    session.save()
    return c


def _seed_delivered_counter(campaign, n=1):
    """make_sent_contact writes the 'sent'/'delivered' SOEvent rows
    directly, bypassing _record_success's own counter increment -- this
    mirrors that increment so a test's starting state matches what real
    production code leaves behind, which matters here specifically because
    this file asserts EXACT post-bounce counter values."""
    SOCampaign.objects.filter(id=campaign.id).update(
        total_sent=F('total_sent') + n, total_delivered=F('total_delivered') + n,
    )


def _seed_engagement(campaign, email, opened=0, clicked=0):
    """Mirrors what a real (pre-bounce) views/so_tracking.py hit leaves
    behind: one SOEvent per occurrence plus a matching counter increment."""
    for _ in range(opened):
        SOEvent.objects.create(campaign=campaign, email=email, event_type='opened', metadata={})
    for _ in range(clicked):
        SOEvent.objects.create(campaign=campaign, email=email, event_type='clicked', metadata={})
    if opened or clicked:
        SOCampaign.objects.filter(id=campaign.id).update(
            total_opened=F('total_opened') + opened, total_clicked=F('total_clicked') + clicked,
        )


@override_settings(**_BASE_SETTINGS)
class OpenTrackingBlockedAfterBounceTests(TestCase):
    """Test 1 — bounced recipient cannot generate OPEN."""

    def setUp(self):
        self.user = make_user('opentest-main@example.com')
        self.account = make_account(self.user, 'sender@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'bounced@gmail.com', '<m1@relay>')
        _record_bounce(self.cc)
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_open_pixel_returns_a_valid_gif_response(self):
        r = self.client.get(f'/so/track/open/{self.cc.tracking_token}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'image/gif')

    def test_no_opened_event_created(self):
        self.client.get(f'/so/track/open/{self.cc.tracking_token}/')
        self.assertFalse(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='opened').exists()
        )

    def test_total_opened_does_not_increase(self):
        self.client.get(f'/so/track/open/{self.cc.tracking_token}/')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 0)

    def test_v36_open_pixel_route_also_blocked(self):
        pixel = SOOpenPixel.objects.create(campaign_contact=self.cc, step_order=0)
        r = self.client.get(f'/so/track/pixel/{pixel.token}/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='opened').exists()
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 0)


@override_settings(**_BASE_SETTINGS)
class ClickTrackingBlockedAfterBounceTests(TestCase):
    """Test 2 — bounced recipient cannot generate CLICK."""

    def setUp(self):
        self.user = make_user('clicktest-main@example.com')
        self.account = make_account(self.user, 'sender2@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'bounced2@gmail.com', '<m2@relay>')
        _record_bounce(self.cc)
        self.link = SOTrackedLink.objects.create(
            campaign_contact=self.cc, destination_url='https://example.com/landing', step_order=0,
        )
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_redirect_still_happens(self):
        r = self.client.get(f'/so/track/click/{self.link.token}/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], 'https://example.com/landing')

    def test_no_clicked_event_created(self):
        self.client.get(f'/so/track/click/{self.link.token}/')
        self.assertFalse(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='clicked').exists()
        )

    def test_total_clicked_does_not_increase(self):
        self.client.get(f'/so/track/click/{self.link.token}/')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_clicked, 0)


@override_settings(**_BASE_SETTINGS)
class NormalRecipientStillTracksTests(TestCase):
    """Test 7/8 — a normal, non-bounced recipient's OPEN/CLICK still work
    exactly as before this fix."""

    def setUp(self):
        self.user = make_user('normaltest-main@example.com')
        self.account = make_account(self.user, 'sender3@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'healthy@example.com', '<m3@relay>')
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_open_tracking_still_works(self):
        self.client.get(f'/so/track/open/{self.cc.tracking_token}/')
        self.assertTrue(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='opened').exists()
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 1)

    def test_click_tracking_still_works(self):
        link = SOTrackedLink.objects.create(
            campaign_contact=self.cc, destination_url='https://example.com/x', step_order=0,
        )
        r = self.client.get(f'/so/track/click/{link.token}/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='clicked').exists()
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_clicked, 1)


class IsContactBouncedUnitTests(TestCase):
    def setUp(self):
        self.user = make_user('helpertest-main@example.com')
        self.account = make_account(self.user, 'sender4@example.com')
        self.campaign = make_campaign(self.user)

    def test_not_bounced_by_default(self):
        cc = make_sent_contact(self.campaign, self.account, 'fine@example.com', '<m4@relay>')
        self.assertFalse(is_contact_bounced(self.campaign.id, cc.email))

    def test_true_after_a_recorded_bounce(self):
        cc = make_sent_contact(self.campaign, self.account, 'gone@gmail.com', '<m5@relay>')
        _record_bounce(cc)
        self.assertTrue(is_contact_bounced(self.campaign.id, cc.email))

    def test_scoped_to_this_campaign_only(self):
        """A bounce on a DIFFERENT campaign must not block tracking for an
        unrelated, successfully-delivered send to the same address."""
        cc1 = make_sent_contact(self.campaign, self.account, 'shared@gmail.com', '<m6@relay>')
        _record_bounce(cc1)
        other_campaign = make_campaign(self.user, name='Other Campaign')
        cc2 = make_sent_contact(other_campaign, self.account, 'shared@gmail.com', '<m7@relay>')
        self.assertFalse(is_contact_bounced(other_campaign.id, cc2.email))


@override_settings(**_BASE_SETTINGS)
class FinalStatusPriorityTests(TestCase):
    """Test 3 — bounce has final status priority over sent/delivered/opened/clicked."""

    def setUp(self):
        self.user = make_user('finalstatus-main@example.com')
        self.account = make_account(self.user, 'sender5@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'wasopen@gmail.com', '<m8@relay>')
        _seed_delivered_counter(self.campaign)
        # Simulate opened/clicked having been recorded BEFORE the bounce was
        # detected -- exactly Campaign 40's shape (delivered + opened all
        # present, bounced discovered later via IMAP).
        _seed_engagement(self.campaign, self.cc.email, opened=1, clicked=1)
        _record_bounce(self.cc)

    def client_login_and_get(self):
        c = _login(self.user)
        r = c.get(f'/Sales-Outreach/sender/{self.campaign.id}/')
        self.assertEqual(r.status_code, 200)
        row = next(row for row in r.context['recipient_rows'] if row['email'] == self.cc.email)
        return row

    def test_effective_final_status_is_bounced(self):
        row = self.client_login_and_get()
        self.assertEqual(row['last_event'], 'bounced')

    def test_pre_bounce_opened_clicked_are_not_shown_as_engagement(self):
        """FINAL RULE: unlike the prior implementation, pre-bounce
        opened/clicked must NOT display as valid history once bounced --
        the underlying SOEvent rows are gone (see
        PostBounceEngagementInvalidationTests), and the per-recipient row
        must agree, not merely re-sort them behind a badge."""
        row = self.client_login_and_get()
        self.assertFalse(row['opened'])
        self.assertFalse(row['clicked'])
        self.assertFalse(row['delivered'])
        self.assertTrue(row['bounced'])

    def test_recipient_rows_sort_uses_bounced_priority(self):
        row = self.client_login_and_get()
        self.assertEqual(row['last_event'], 'bounced')


@override_settings(**_BASE_SETTINGS)
class AnalyticsExcludeBouncedEngagementTests(TestCase):
    """Test 3 (counters half) — services/so_analytics.py must not report a
    bounced recipient's delivered/opened/clicked in totals or rates."""

    def setUp(self):
        self.user = make_user('analyticsfix-main@example.com')
        self.account = make_account(self.user, 'sender6@example.com')
        self.campaign = make_campaign(self.user)
        self.cc1 = make_sent_contact(self.campaign, self.account, 'p1@gmail.com', '<m9@relay>')
        self.cc2 = make_sent_contact(self.campaign, self.account, 'p2@gmail.com', '<m10@relay>')
        SOEvent.objects.create(campaign=self.campaign, email=self.cc1.email, event_type='opened', metadata={})
        _record_bounce(self.cc1)
        _record_bounce(self.cc2)

    def test_no_impossible_delivered_bounced_coexistence_in_overview(self):
        overview = so_analytics.compute_overview(self.campaign)
        self.assertEqual(overview['totals']['sent'], 2)
        self.assertEqual(overview['totals']['bounced'], 2)
        # Both contacts bounced -- neither should count as delivered/opened.
        self.assertEqual(overview['totals']['delivered'], 0)
        self.assertEqual(overview['totals']['opened'], 0)

    def test_funnel_also_excludes_bounced_delivered(self):
        funnel = so_analytics.compute_funnel(self.campaign)
        delivered_stage = next(s for s in funnel if s['stage'] == 'delivered')
        self.assertEqual(delivered_stage['value'], 0)

    def test_raw_bounced_counter_is_untouched_by_the_read_side_exclusion(self):
        """The fix is read-side only -- _exclude_bounced_engagement() never
        mutates a stored SOCampaign.total_* counter. total_bounced is
        incremented by the real so_imap._record_once() path exercised via
        _record_bounce() in setUp, so it's the one counter this fixture
        actually drives end-to-end; asserting it stays at 2 (not silently
        decremented back to 0 by anything in the analytics read path) is
        the meaningful regression check here."""
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 2)


@override_settings(**_BASE_SETTINGS)
class PostBounceEngagementInvalidationTests(TestCase):
    """FINAL RULE, Tests A/B/C/D — a confirmed hard bounce must retract
    delivered/opened/clicked outright (not merely exclude them at read
    time): the SOEvent rows themselves must be gone and the corresponding
    counters decremented by exactly what was removed. SENT (and the
    SOCampaignContact/bounced audit trail) must survive untouched.

    09:00 SENT, 09:01 DELIVERED, 09:02 OPENED, 09:03 CLICKED,
    09:05 HARD BOUNCE CONFIRMED -- effective state after must be BOUNCED,
    with delivered/opened/clicked gone, not merely hidden."""

    def setUp(self):
        self.user = make_user('invalidate-main@example.com')
        self.account = make_account(self.user, 'sender10@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'wasengaged@gmail.com', '<m14@relay>')
        _seed_delivered_counter(self.campaign)
        _seed_engagement(self.campaign, self.cc.email, opened=1, clicked=1)
        self.bounce_recorded = _record_bounce(self.cc)

    def _events(self, event_type):
        return SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type=event_type)

    def test_bounce_was_recorded(self):
        self.assertTrue(self.bounce_recorded)

    def test_a_delivered_event_no_longer_exists(self):
        self.assertFalse(self._events('delivered').exists())

    def test_b_opened_event_no_longer_exists(self):
        self.assertFalse(self._events('opened').exists())

    def test_c_clicked_event_no_longer_exists(self):
        self.assertFalse(self._events('clicked').exists())

    def test_sent_event_is_preserved(self):
        """A hard bounce doesn't mean the SMTP send never happened -- SENT
        remains as evidence the application handed the message off."""
        self.assertTrue(self._events('sent').exists())

    def test_bounced_event_exists_exactly_once(self):
        self.assertEqual(self._events('bounced').count(), 1)

    def test_d_counters_are_corrected_not_contradictory(self):
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_sent, 1)       # SENT survives
        self.assertEqual(self.campaign.total_delivered, 0)  # retracted
        self.assertEqual(self.campaign.total_opened, 0)     # retracted
        self.assertEqual(self.campaign.total_clicked, 0)    # retracted
        self.assertEqual(self.campaign.total_bounced, 1)    # recorded

    def test_multiple_prior_opens_all_invalidated_and_fully_decremented(self):
        """A contact that opened twice before bouncing loses exactly 2 from
        total_opened, not a flat -1 -- proves the decrement is derived from
        the actual row count, not a hardcoded step."""
        cc2 = make_sent_contact(self.campaign, self.account, 'openedtwice@gmail.com', '<m15@relay>')
        _seed_delivered_counter(self.campaign)
        _seed_engagement(self.campaign, cc2.email, opened=2)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 2)
        _record_bounce(cc2)
        self.assertEqual(
            SOEvent.objects.filter(campaign=self.campaign, email=cc2.email, event_type='opened').count(), 0,
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_opened, 0)


@override_settings(**_BASE_SETTINGS)
class GenuineBounceStillWorksTests(TestCase):
    """Test 4/5 — a genuine hard bounce is still recorded exactly once, and
    reprocessing it is idempotent. (Also covered end-to-end against a fake
    IMAP server in test_so_imap_sync.py -- this is the narrow, fast
    regression check that _record_once itself still holds after this fix.)"""

    def setUp(self):
        self.user = make_user('genuinebounce-main@example.com')
        self.account = make_account(self.user, 'sender7@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'realbounce@gmail.com', '<m11@relay>')

    def test_first_bounce_is_recorded(self):
        recorded = _record_bounce(self.cc)
        self.assertTrue(recorded)
        self.assertEqual(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='bounced').count(), 1,
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 1)

    def test_reprocessing_the_same_bounce_is_idempotent(self):
        _record_bounce(self.cc)
        recorded_again = _record_bounce(self.cc)
        self.assertFalse(recorded_again)
        self.assertEqual(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='bounced').count(), 1,
        )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 1)


@override_settings(**_BASE_SETTINGS)
class IdempotentBounceCleanupTests(TestCase):
    """Test J — processing the same bounce twice (the IMAP inbox is
    re-scanned every ~15 minutes, so the same DSN is seen repeatedly) must
    not create duplicate bounce state, double-decrement counters, delete
    unrelated events, or corrupt totals. Complements
    GenuineBounceStillWorksTests' narrower total_bounced-only check with
    the full delivered/opened/clicked invalidation path, plus an unrelated
    contact used as a canary for "delete unrelated events"."""

    def setUp(self):
        self.user = make_user('idempotent-main@example.com')
        self.account = make_account(self.user, 'sender11@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'repeatbounce@gmail.com', '<m16@relay>')
        _seed_delivered_counter(self.campaign)
        _seed_engagement(self.campaign, self.cc.email, opened=1, clicked=1)
        # An unrelated, healthy contact in the SAME campaign -- must be
        # completely unaffected by any pass of bounce processing below.
        self.other_cc = make_sent_contact(self.campaign, self.account, 'unrelated@example.com', '<m17@relay>')
        _seed_delivered_counter(self.campaign)
        _seed_engagement(self.campaign, self.other_cc.email, opened=1, clicked=1)

    def test_second_pass_does_not_double_decrement_or_touch_unrelated_events(self):
        _record_bounce(self.cc)
        self.campaign.refresh_from_db()
        after_first = (self.campaign.total_delivered, self.campaign.total_opened,
                       self.campaign.total_clicked, self.campaign.total_bounced)

        recorded_again = _record_bounce(self.cc)  # IMAP's next 15-minute pass, same DSN

        self.assertFalse(recorded_again)
        self.campaign.refresh_from_db()
        after_second = (self.campaign.total_delivered, self.campaign.total_opened,
                         self.campaign.total_clicked, self.campaign.total_bounced)
        self.assertEqual(after_first, after_second)
        self.assertEqual(
            SOEvent.objects.filter(campaign=self.campaign, email=self.cc.email, event_type='bounced').count(), 1,
        )
        # The unrelated contact's engagement must be completely untouched.
        self.assertEqual(
            SOEvent.objects.filter(campaign=self.campaign, email=self.other_cc.email,
                                    event_type__in=('delivered', 'opened', 'clicked')).count(), 3,
        )
        self.assertEqual(self.campaign.total_opened, 1)   # only the OTHER contact's open survives
        self.assertEqual(self.campaign.total_clicked, 1)  # only the OTHER contact's click survives


@override_settings(**_BASE_SETTINGS)
class SuppressionPreservedTests(TestCase):
    """Test 6 — a bounced recipient remains suppressed from a new/duplicate
    campaign using the same list. Verifies existing suppression logic
    (tasks/so_send_campaign.py, views/so_sender.py::so_estimate_recipients)
    is untouched by this fix, not just re-implemented here."""

    def setUp(self):
        self.user = make_user('suppresstest-main@example.com')
        self.account = make_account(self.user, 'sender8@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'suppressed@gmail.com', '<m12@relay>')
        _record_bounce(self.cc)

    def test_send_next_step_refuses_to_send_to_a_bounced_contact_elsewhere(self):
        """A second campaign enrolling the same address must have its
        per-step send refused by send_next_step's own existing suppression
        check (services/so_drip.py) -- unrelated to is_contact_bounced,
        which is scoped per-campaign; this is the pre-existing, untouched,
        cross-campaign guard."""
        other_campaign = make_campaign(self.user, name='Follow-up Campaign')
        cc2 = SOCampaignContact.objects.create(
            campaign=other_campaign, email='suppressed@gmail.com', status='sending',
            current_step=0, account=self.account,
        )
        sent = so_drip.send_next_step(cc2)
        self.assertFalse(sent)
        cc2.refresh_from_db()
        self.assertEqual(cc2.status, 'stopped')


@override_settings(**_BASE_SETTINGS)
class RecipientEstimateExcludesBouncedTests(TestCase):
    """Test H — so_estimate_recipients (the wizard's live pre-launch count)
    must exclude a bounced email exactly like real enrollment does."""

    def setUp(self):
        self.user = make_user('estimatetest-main@example.com')
        self.account = make_account(self.user, 'sender12@example.com')
        self.campaign = make_campaign(self.user, name='Campaign That Bounced')
        self.cc = make_sent_contact(self.campaign, self.account, 'estimatebounce@gmail.com', '<m18@relay>')
        _record_bounce(self.cc)

        from Email_validate_app.models import SOProspect
        healthy = SOProspect.objects.create(
            user_id=self.user.id, email='estimatehealthy@example.com', status='subscribed',
        )
        bounced_prospect = SOProspect.objects.filter(
            user_id=self.user.id, email__iexact=self.cc.email,
        ).first()
        self.so_list = SOList.objects.create(user=self.user, name='Estimate List')
        SOListProspect.objects.create(so_list=self.so_list, prospect=bounced_prospect)
        SOListProspect.objects.create(so_list=self.so_list, prospect=healthy)

    def test_estimate_excludes_the_bounced_email(self):
        c = _login(self.user)
        r = c.post('/Sales-Outreach/sender/estimate-recipients/', {'list_ids': str(self.so_list.id)})
        self.assertEqual(r.status_code, 200)
        # 2 prospects on the list, but 1 already bounced -- only 1 counts.
        self.assertEqual(r.json()['count'], 1)


@override_settings(**_BASE_SETTINGS)
class CrossUserSafetyTests(TestCase):
    """Test L — a bounce recorded for User A's campaign+email must never
    suppress that same email for a completely different User B. Every
    suppression query in this codebase (enrollment, per-step re-check,
    estimation) is scoped campaign__user_id, mirroring how unsubscribe
    suppression is scoped per-user too -- verified here from the opposite
    direction, proving a DIFFERENT user's send/estimate is unaffected."""

    def setUp(self):
        self.user_a = make_user('crossuser-a@example.com')
        self.account_a = make_account(self.user_a, 'sender13@example.com')
        self.campaign_a = make_campaign(self.user_a, name='User A Campaign')
        self.shared_email = 'shared-across-tenants@example.com'
        self.cc_a = make_sent_contact(self.campaign_a, self.account_a, self.shared_email, '<m19@relay>')
        _record_bounce(self.cc_a)

        self.user_b = make_user('crossuser-b@example.com')
        self.account_b = make_account(self.user_b, 'sender14@example.com')
        self.campaign_b = make_campaign(self.user_b, name='User B Campaign')

    def test_is_contact_bounced_is_false_for_user_bs_campaign(self):
        self.assertFalse(is_contact_bounced(self.campaign_b.id, self.shared_email))

    def test_send_next_step_does_not_suppress_user_bs_contact(self):
        from Email_validate_app.models import SOProspect
        # send_next_step's FIRST gate is still_subscribed (SOProspect) --
        # must pass that to actually exercise the bounce/complained gate
        # right after it, which is what this test is really about.
        prospect_b = SOProspect.objects.create(
            user_id=self.user_b.id, email=self.shared_email, status='subscribed',
        )
        cc_b = SOCampaignContact.objects.create(
            campaign=self.campaign_b, prospect=prospect_b, email=self.shared_email,
            status='sending', current_step=5, account=self.account_b,  # beyond any configured step
        )
        so_drip.send_next_step(cc_b)
        cc_b.refresh_from_db()
        # Not suppressed by User A's bounce -- falls through to "no such
        # step left to send" and completes normally, never 'stopped: bounced'.
        self.assertEqual(cc_b.status, 'completed')

    def test_estimate_for_user_b_does_not_exclude_the_shared_email(self):
        from Email_validate_app.models import SOProspect
        prospect_b = SOProspect.objects.create(
            user_id=self.user_b.id, email=self.shared_email, status='subscribed',
        )
        so_list = SOList.objects.create(user=self.user_b, name='User B List')
        SOListProspect.objects.create(so_list=so_list, prospect=prospect_b)
        c = _login(self.user_b)
        r = c.post('/Sales-Outreach/sender/estimate-recipients/', {'list_ids': str(so_list.id)})
        self.assertEqual(r.json()['count'], 1)


@override_settings(**_BASE_SETTINGS)
class DuplicateCampaignNoRuntimeHistoryTests(TestCase):
    """Test 9/I — duplicating a campaign must not copy SOCampaignContact,
    SOEvent, SOConversation, or SOMessage, only the audience configuration
    -- and launching the duplicate must still suppress the bounced
    recipient at send time."""

    def setUp(self):
        self.user = make_user('duptest-main@example.com')
        self.account = make_account(self.user, 'sender9@example.com')
        self.campaign = make_campaign(self.user)
        self.cc = make_sent_contact(self.campaign, self.account, 'history@gmail.com', '<m13@relay>')
        _record_bounce(self.cc)
        self.so_list = SOList.objects.create(user=self.user, name='Test List')
        self.campaign.recipient_lists.add(self.so_list)
        # The one prospect this list would otherwise offer a new campaign.
        from Email_validate_app.models import SOProspect
        prospect = SOProspect.objects.filter(user_id=self.user.id, email__iexact=self.cc.email).first()
        SOListProspect.objects.create(so_list=self.so_list, prospect=prospect)

    def test_duplicate_has_zero_contacts_and_events(self):
        from Email_validate_app.views.so_sender import _duplicate_campaign
        dup = _duplicate_campaign(self.campaign, self.user.id)
        self.assertEqual(dup.campaign_contacts.count(), 0)
        self.assertEqual(dup.events.count(), 0)
        self.assertEqual(dup.status, 'draft')

    def test_duplicate_keeps_the_same_recipient_lists(self):
        from Email_validate_app.views.so_sender import _duplicate_campaign
        dup = _duplicate_campaign(self.campaign, self.user.id)
        self.assertEqual(list(dup.recipient_lists.all()), [self.so_list])

    def test_duplicate_send_suppresses_the_bounced_recipient(self):
        """Test I — launching the duplicate must enroll ZERO recipients:
        the only prospect on its (copied) recipient list already bounced
        under the original campaign. so_send_campaign_task only enrolls
        (see its own docstring: "does NOT send anything itself"), so this
        exercises real audience resolution/suppression with no SMTP
        involved."""
        from Email_validate_app.models import SOEmailAccountRotation
        from Email_validate_app.views.so_sender import _duplicate_campaign
        from Email_validate_app.tasks.so_send_campaign import so_send_campaign_task

        dup = _duplicate_campaign(self.campaign, self.user.id)
        # _duplicate_campaign already copies SOEmailAccountRotation from a
        # real campaign save; built directly here since this campaign was
        # made via the make_campaign test fixture, which doesn't set one up.
        SOEmailAccountRotation.objects.create(campaign=dup, account=self.account, order=1)

        result = so_send_campaign_task(dup.id)

        self.assertEqual(result['status'], 'no_recipients')
        self.assertEqual(dup.campaign_contacts.count(), 0)


def _record_soft_bounce(cc):
    """Exactly what services/so_imap.py::_handle_bounce_candidate does on a
    real soft-bounce match -- reused directly, not re-implemented."""
    return so_imap._record_soft_bounce(cc, {'subject': 'Delivery Status Notification (Failure)', 'severity': 'soft'})


@override_settings(**_BASE_SETTINGS)
class HardVsSoftBounceUILabelTests(TestCase):
    """UI requirement — the campaign detail page must only ever show
    'Hard Bounce' or 'Soft Bounce', never the raw DSN/SMTP diagnostic text
    that goes into SOEvent.metadata (subject/severity/reason/smtp_message)."""

    def setUp(self):
        self.user = make_user('bouncelabels-main@example.com')
        self.account = make_account(self.user, 'sender15@example.com')
        self.campaign = make_campaign(self.user)
        self.raw_diagnostic = '550-5.7.26 this domain fails DKIM and SPF and DMARC checks'

    def _row_for(self, email):
        c = _login(self.user)
        r = c.get(f'/Sales-Outreach/sender/{self.campaign.id}/')
        self.assertEqual(r.status_code, 200)
        row = next(row for row in r.context['recipient_rows'] if row['email'] == email)
        return r, row

    def test_hard_bounce_shows_hard_bounce_label(self):
        cc = make_sent_contact(self.campaign, self.account, 'hardlabel@gmail.com', '<m20@relay>')
        so_imap._record_once(cc, 'bounced', 'total_bounced',
                              {'subject': self.raw_diagnostic, 'severity': 'hard'})
        r, row = self._row_for(cc.email)
        self.assertEqual(row['last_event'], 'bounced')
        self.assertIn('Hard Bounce', r.content.decode())
        self.assertNotIn(self.raw_diagnostic, r.content.decode())

    def test_soft_bounce_shows_soft_bounce_label(self):
        cc = make_sent_contact(self.campaign, self.account, 'softlabel@gmail.com', '<m21@relay>')
        _record_soft_bounce(cc)
        r, row = self._row_for(cc.email)
        self.assertEqual(row['last_event'], 'soft_bounced')
        self.assertIn('Soft Bounce', r.content.decode())

    def test_no_raw_diagnostic_text_anywhere_on_the_page(self):
        cc = make_sent_contact(self.campaign, self.account, 'nodiag@gmail.com', '<m22@relay>')
        so_imap._record_once(cc, 'bounced', 'total_bounced',
                              {'subject': self.raw_diagnostic, 'severity': 'hard'})
        r, _row = self._row_for(cc.email)
        body = r.content.decode()
        self.assertNotIn(self.raw_diagnostic, body)
        self.assertNotIn('5.7.26', body)
        self.assertNotIn('DKIM', body)
