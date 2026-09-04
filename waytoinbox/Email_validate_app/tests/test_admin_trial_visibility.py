"""Admin visibility into the 7-Day Free Trial system: the Trial filter and
badge on the user list page, and the Free Trial card on the user detail
page (services/admin/user_service.py, templates/admin/users/{list,detail}.html).
"""
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils.timezone import now

from Email_validate_app.models import UserTable
from Email_validate_app.services.trial_manager import TRIAL_LIMITS, activate_trial


def make_user(email, admin=False, verified=True):
    user = UserTable.objects.create_user(
        user_name='Trial Admin Test', user_email=email, password='StrongPass123!')
    user.is_verified = verified
    user.is_admin = admin
    user.save(update_fields=['is_verified', 'is_admin'])
    return user


def _backdate_trial(user):
    started = now() - timedelta(days=8)
    user.trial_started_at = started
    user.trial_ends_at = started + timedelta(days=7)
    user.save(update_fields=['trial_started_at', 'trial_ends_at'])
    return user


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class AdminTrialVisibilityTests(TestCase):

    def setUp(self):
        self.admin = make_user('trial_admin@example.com', admin=True)
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.admin.user_email
        session['is_admin'] = True
        session.save()

    # ── list page ──────────────────────────────────────────────────────────

    def test_active_trial_user_shows_the_trial_badge_on_the_list_page(self):
        user = make_user('trial_active_list@example.com')
        activate_trial(user)

        html = self.client.get('/wti-admin/users/').content.decode()

        self.assertIn('Trial', html)  # the badge text itself

    def test_never_trialed_user_shows_no_trial_badge(self):
        make_user('trial_none_list@example.com')

        html = self.client.get('/wti-admin/users/').content.decode()

        # The admin's own row plus this one -- neither has an active trial,
        # so the trial badge's own icon must not appear on the page at all.
        self.assertNotIn('fa-hourglass-half', html)

    def test_trial_filter_active_only_returns_users_with_an_active_trial(self):
        active_user = make_user('trial_filter_active@example.com')
        activate_trial(active_user)
        expired_user = make_user('trial_filter_expired@example.com')
        activate_trial(expired_user)
        _backdate_trial(expired_user)
        never_user = make_user('trial_filter_none@example.com')

        html = self.client.get('/wti-admin/users/', {'trial': 'active'}).content.decode()

        self.assertIn(active_user.user_email, html)
        self.assertNotIn(expired_user.user_email, html)
        self.assertNotIn(never_user.user_email, html)

    def test_trial_filter_expired_only_returns_expired_trials(self):
        active_user = make_user('trial_filter2_active@example.com')
        activate_trial(active_user)
        expired_user = make_user('trial_filter2_expired@example.com')
        activate_trial(expired_user)
        _backdate_trial(expired_user)

        html = self.client.get('/wti-admin/users/', {'trial': 'expired'}).content.decode()

        self.assertIn(expired_user.user_email, html)
        self.assertNotIn(active_user.user_email, html)

    def test_trial_filter_none_excludes_anyone_who_ever_trialed(self):
        trialed_user = make_user('trial_filter3_trialed@example.com')
        activate_trial(trialed_user)
        never_user = make_user('trial_filter3_never@example.com')

        html = self.client.get('/wti-admin/users/', {'trial': 'none'}).content.decode()

        self.assertIn(never_user.user_email, html)
        self.assertNotIn(trialed_user.user_email, html)

    # ── detail page ────────────────────────────────────────────────────────

    def test_detail_page_for_a_never_trialed_user_shows_the_empty_state(self):
        user = make_user('trial_detail_none@example.com')

        html = self.client.get(f'/wti-admin/users/{user.pk}/').content.decode()

        self.assertIn('Never Activated', html)
        self.assertNotIn('days left', html)
        self.assertNotIn('>Expired<', html)

    def test_detail_page_for_an_active_trial_shows_days_left_and_per_service_usage(self):
        user = make_user('trial_detail_active@example.com')
        activate_trial(user)
        from Email_validate_app.services.credit_manager import deduct_service_credits
        deduct_service_credits(user.id, 'sales_outreach', 1, ref_type='so_account')

        html = self.client.get(f'/wti-admin/users/{user.pk}/').content.decode()

        self.assertIn('days left', html)
        # The Sales Outreach row shows 1 used out of its configured limit.
        self.assertIn(f'1<span', html)
        self.assertIn(str(TRIAL_LIMITS['sales_outreach']), html)
        self.assertIn('Email Validation', html)
        self.assertIn('Domain Blocklist Monitor', html)

    def test_detail_page_for_an_expired_trial_shows_expired_not_active(self):
        user = make_user('trial_detail_expired@example.com')
        activate_trial(user)
        _backdate_trial(user)

        html = self.client.get(f'/wti-admin/users/{user.pk}/').content.decode()

        self.assertIn('Expired', html)
        self.assertNotIn('days left', html)
