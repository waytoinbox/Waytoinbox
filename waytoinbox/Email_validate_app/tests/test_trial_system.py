"""Tests for the 7-Day Free Trial system.

Covers: services/trial_manager.py (eligibility/manual activation/remaining),
credit_manager.py's trial-aware get_effective_balance()/deduct_service_credits(),
the manual activation endpoint (views/credits.py::trial_activate), the
signup()-creates-a-session change (views/auth.py), and
tasks/scheduler_job.py::trial_expiry_notification_job().

Activation is manual only: verify_email() does NOT activate a trial anymore
-- only trial_activate() does, and only for a verified, eligible user.

Follows this project's established TestCase conventions (see
test_service_credits.py, test_expiry_retention.py) -- Django's isolated test
database only, never the real one.
"""
from datetime import timedelta

from django.contrib.auth.tokens import default_token_generator
from django.test import Client, TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.timezone import now

from Email_validate_app.models import (
    CurrentCredits, ServiceCredit, ServiceTrial, SubsPayment, TrialUsageLog,
    UserTable, SERVICE_KEYS,
)
from Email_validate_app.services.credit_manager import (
    InsufficientCredits, deduct_service_credits, get_effective_balance,
)
from Email_validate_app.services.trial_manager import (
    TRIAL_LIMITS, activate_trial, can_offer_trial, get_trial_remaining,
    has_ever_paid, is_trial_active, is_trial_eligible,
)
from Email_validate_app.tasks.scheduler_job import (
    subscription_expiry_job, trial_expiry_notification_job,
)


def make_user(email, verified=False):
    user = UserTable.objects.create_user(
        user_name='Trial Test', user_email=email, password='StrongPass123!')
    if verified:
        user.is_verified = True
        user.save(update_fields=['is_verified'])
    return user


def _backdate_trial(user, days_ago=1):
    """Simulate a trial that started 8 days ago and has already elapsed."""
    started = now() - timedelta(days=7 + days_ago)
    user.trial_started_at = started
    user.trial_ends_at = started + timedelta(days=7)
    user.save(update_fields=['trial_started_at', 'trial_ends_at'])
    return user


class ActivateTrialTests(TestCase):

    def test_eligible_user_can_activate(self):
        user = make_user('trial_new@example.com')
        self.assertTrue(is_trial_eligible(user))

        result = activate_trial(user)

        self.assertTrue(result)
        user.refresh_from_db()
        self.assertIsNotNone(user.trial_started_at)
        self.assertIsNotNone(user.trial_ends_at)
        self.assertEqual((user.trial_ends_at - user.trial_started_at).days, 7)
        self.assertTrue(is_trial_active(user))

    def test_activation_creates_all_seven_service_trial_rows_at_correct_limits(self):
        user = make_user('trial_seven@example.com')
        activate_trial(user)

        rows = {row.service: row for row in ServiceTrial.objects.filter(user_id=user.id)}
        self.assertEqual(set(rows), set(SERVICE_KEYS))
        for service in SERVICE_KEYS:
            self.assertEqual(rows[service].limit, TRIAL_LIMITS[service])
            self.assertEqual(rows[service].used, 0)

    def test_activation_writes_seven_granted_log_entries(self):
        user = make_user('trial_log@example.com')
        activate_trial(user)

        granted = TrialUsageLog.objects.filter(user_id=user.id, entry_type='granted')
        self.assertEqual(granted.count(), 7)
        for row in granted:
            self.assertEqual(row.amount, TRIAL_LIMITS[row.service])
            self.assertEqual(row.balance_after, TRIAL_LIMITS[row.service])

    # ── one trial per lifetime ────────────────────────────────────────────

    def test_second_activation_is_a_no_op(self):
        user = make_user('trial_once@example.com')
        activate_trial(user)
        user.refresh_from_db()
        first_started = user.trial_started_at

        result = activate_trial(user)

        self.assertFalse(result)
        user.refresh_from_db()
        self.assertEqual(user.trial_started_at, first_started)
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 7)
        self.assertEqual(
            TrialUsageLog.objects.filter(user_id=user.id, entry_type='granted').count(), 7)

    def test_ineligible_user_is_not_re_queried_and_returns_false_cheaply(self):
        user = make_user('trial_ineligible@example.com')
        activate_trial(user)
        user.refresh_from_db()

        # is_trial_eligible is a pure check against the already-loaded object
        self.assertFalse(is_trial_eligible(user))
        self.assertFalse(activate_trial(user))

    def test_updated_trial_limits_are_the_new_values(self):
        """Guard against TRIAL_LIMITS being changed without meaning to."""
        user = make_user('trial_new_limits@example.com')
        activate_trial(user)
        rows = {r.service: r.limit for r in ServiceTrial.objects.filter(user_id=user.id)}
        self.assertEqual(rows['email_validation'], 100)
        self.assertEqual(rows['email_marketing'], 200)
        self.assertEqual(rows['header_analysis'], 25)
        self.assertEqual(rows['sales_outreach'], 2)
        self.assertEqual(rows['reputation'], 2)
        self.assertEqual(rows['ip_blocklist'], 5)
        self.assertEqual(rows['domain_blocklist'], 5)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class VerifyEmailNeverActivatesTrialTests(TestCase):
    """The trial must never activate automatically. Clicking the emailed
    verification link only unlocks eligibility to activate -- it must not
    start the trial itself. See TrialActivateEndpointTests for the only
    real activation path."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _verify_url(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return f'/verify/{uidb64}/{token}/'

    def test_clicking_the_verification_link_verifies_but_does_not_start_a_trial(self):
        user = make_user('trial_verify@example.com', verified=False)
        self.assertIsNone(user.trial_started_at)

        self.client.get(self._verify_url(user))

        user.refresh_from_db()
        self.assertTrue(user.is_verified)
        self.assertIsNone(user.trial_started_at)
        self.assertIsNone(user.trial_ends_at)
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 0)
        self.assertEqual(TrialUsageLog.objects.filter(user_id=user.id).count(), 0)
        # Still eligible -- verifying only unlocks activation, doesn't spend it.
        self.assertTrue(is_trial_eligible(user))

    def test_verifying_an_already_verified_user_still_grants_no_trial(self):
        user = make_user('trial_reverify@example.com', verified=True)

        self.client.get(self._verify_url(user))

        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class TrialActivateEndpointTests(TestCase):
    """POST /trial/activate/ -- the ONLY way a trial starts."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _login_session(self, user):
        session = self.client.session
        session['logged_in'] = user.user_email
        session.save()

    def test_not_authenticated_returns_401(self):
        r = self.client.post('/trial/activate/')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['reason'], 'not_authenticated')

    def test_unverified_user_returns_403_not_verified(self):
        user = make_user('trial_ep_unverified@example.com', verified=False)
        self._login_session(user)

        r = self.client.post('/trial/activate/')

        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['reason'], 'not_verified')
        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)

    def test_verified_eligible_user_activates_successfully(self):
        user = make_user('trial_ep_ok@example.com', verified=True)
        self._login_session(user)

        r = self.client.post('/trial/activate/')

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data['status'], 'ok')
        self.assertIn('trial_ends_at', data)
        limits = {row['service']: row['limit'] for row in data['services']}
        self.assertEqual(limits, TRIAL_LIMITS)
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 7)
        user.refresh_from_db()
        self.assertIsNotNone(user.trial_started_at)

    def test_already_used_returns_409(self):
        user = make_user('trial_ep_used@example.com', verified=True)
        activate_trial(user)
        self._login_session(user)

        r = self.client.post('/trial/activate/')

        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['reason'], 'already_used')
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 7)

    def test_get_method_not_allowed(self):
        user = make_user('trial_ep_get@example.com', verified=True)
        self._login_session(user)
        r = self.client.get('/trial/activate/')
        self.assertEqual(r.status_code, 405)

    def test_already_paid_but_never_trialed_returns_409_already_paid(self):
        """Per the confirmed product rule, a real payment retires the trial
        offer even for someone who never started a trial at all."""
        from Email_validate_app.models import Payment
        user = make_user('trial_ep_paid_no_trial@example.com', verified=True)
        Payment.objects.create(
            user=user, order_id='order_ep_paid_no_trial', payment_id='pay_ep_paid_no_trial',
            amount='9.99',
        )
        self._login_session(user)

        r = self.client.post('/trial/activate/')

        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['reason'], 'already_paid')
        self.assertEqual(ServiceTrial.objects.filter(user_id=user.id).count(), 0)
        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)

    def test_paid_via_subs_payment_only_also_returns_409_already_paid(self):
        user = make_user('trial_ep_subs_paid@example.com', verified=True)
        SubsPayment.objects.create(
            user=user, order_id='order_ep_subs_paid', payment_id='pay_ep_subs_paid',
            amount='19.99',
        )
        self._login_session(user)

        r = self.client.post('/trial/activate/')

        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['reason'], 'already_paid')


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class SignupCreatesSessionTests(TestCase):
    """signup() now starts a session immediately, even while unverified, so
    an unverified user can reach session-gated pages (the trial popup on
    Pricing/Subscription) in the same tab. login()'s own verified-only gate
    is untouched, and trial_activate()/purchase endpoints separately
    re-check is_verified -- a session alone grants browsing only."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        # signup() rate-limits by IP (5 attempts / 10 min, cache-backed, see
        # views/auth.py::_rate_check) -- this class's several test methods
        # all POST to /signup/ from the same test-client IP, which would
        # otherwise trip that limiter partway through this class's own
        # tests. Clearing the cache is safe and DB-free.
        from django.core.cache import cache
        cache.clear()

    def test_signup_response_sets_a_logged_in_session(self):
        r = self.client.post('/signup/', {
            'user_name': 'New Signup',
            'user_email': 'signup_session@example.com',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(self.client.session.get('logged_in'), 'signup_session@example.com')
        user = UserTable.objects.get(user_email='signup_session@example.com')
        self.assertFalse(user.is_verified)

    def test_session_from_signup_grants_access_to_a_gated_page_while_unverified(self):
        self.client.post('/signup/', {
            'user_name': 'New Signup',
            'user_email': 'signup_session_pricing@example.com',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        r = self.client.get('/pricing/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('/login/', getattr(r, 'url', ''))

    def test_trial_activate_still_blocked_for_the_signup_session_until_verified(self):
        self.client.post('/signup/', {
            'user_name': 'New Signup',
            'user_email': 'signup_session_trial@example.com',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        r = self.client.post('/trial/activate/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['reason'], 'not_verified')

    def test_resend_verification_branch_also_creates_a_session(self):
        make_user('signup_resend@example.com', verified=False)
        r = self.client.post('/signup/', {
            'user_name': 'Resend',
            'user_email': 'signup_resend@example.com',
            'password': 'StrongPass123!',
        })
        self.assertEqual(r.json()['status'], 'info')
        self.assertEqual(self.client.session.get('logged_in'), 'signup_resend@example.com')

    def test_unverified_session_still_blocked_from_purchasing(self):
        self.client.post('/signup/', {
            'user_name': 'New Signup',
            'user_email': 'signup_session_purchase@example.com',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        r = self.client.post('/subscription/order/',
                              data='{"cart": {}}', content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['reason'], 'not_verified')


class TrialUsageGatingTests(TestCase):
    """get_trial_remaining() / get_effective_balance() / deduct_service_credits()
    with an active trial -- the credit_manager.py integration."""

    def setUp(self):
        self.user = make_user('trial_gate@example.com')
        activate_trial(self.user)

    # ── all 7 services independently limited ─────────────────────────────

    def test_services_are_independent_spending_one_does_not_touch_another(self):
        deduct_service_credits(self.user.id, 'sales_outreach', 1, ref_type='validation')

        self.assertEqual(get_trial_remaining(self.user.id, 'sales_outreach'), 1)  # limit 2, spent 1
        self.assertEqual(get_trial_remaining(self.user.id, 'email_validation'), 100)
        self.assertEqual(get_trial_remaining(self.user.id, 'email_marketing'), 200)

    def test_all_seven_start_at_their_configured_limit(self):
        for service in SERVICE_KEYS:
            self.assertEqual(get_trial_remaining(self.user.id, service), TRIAL_LIMITS[service])
            self.assertEqual(get_effective_balance(self.user.id, service), TRIAL_LIMITS[service])

    # ── exact boundary behaviour ──────────────────────────────────────────

    def test_hundredth_email_validation_credit_succeeds_101st_fails(self):
        deduct_service_credits(self.user.id, 'email_validation', 100, ref_type='validation')
        self.assertEqual(get_trial_remaining(self.user.id, 'email_validation'), 0)

        with self.assertRaises(InsufficientCredits) as ctx:
            deduct_service_credits(self.user.id, 'email_validation', 1, ref_type='validation')
        self.assertTrue(ctx.exception.trial_active)
        self.assertTrue(ctx.exception.trial_exhausted)

    def test_two_credit_service_allows_two_and_rejects_the_third(self):
        deduct_service_credits(self.user.id, 'sales_outreach', 2, ref_type='so_account')
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'sales_outreach', 1, ref_type='so_account')

    def test_header_analyzer_twentyfive_credit_boundary(self):
        deduct_service_credits(self.user.id, 'header_analysis', 25, ref_type='ip_check')
        self.assertEqual(get_trial_remaining(self.user.id, 'header_analysis'), 0)
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'header_analysis', 1, ref_type='ip_check')

    # ── spend order: trial first, ahead of paid wallet ────────────────────

    def test_trial_is_spent_before_the_paid_wallet(self):
        ServiceCredit.objects.create(user_id=self.user.id, service='email_validation', balance=100)

        deduct_service_credits(self.user.id, 'email_validation', 30, ref_type='validation')

        self.assertEqual(get_trial_remaining(self.user.id, 'email_validation'), 70)
        self.assertEqual(
            ServiceCredit.objects.get(user_id=self.user.id, service='email_validation').balance, 100,
            "paid wallet must be untouched while trial still has enough")

    def test_falls_through_to_paid_wallet_once_trial_is_exhausted(self):
        ServiceCredit.objects.create(user_id=self.user.id, service='sales_outreach', balance=5)

        deduct_service_credits(self.user.id, 'sales_outreach', 2, ref_type='so_account')  # trial's full 2
        deduct_service_credits(self.user.id, 'sales_outreach', 3, ref_type='so_account')  # from paid wallet

        self.assertEqual(get_trial_remaining(self.user.id, 'sales_outreach'), 0)
        self.assertEqual(
            ServiceCredit.objects.get(user_id=self.user.id, service='sales_outreach').balance, 2)

    # ── usage logging ──────────────────────────────────────────────────────

    def test_a_spend_writes_exactly_one_debit_log_row_with_correct_before_after(self):
        deduct_service_credits(self.user.id, 'reputation', 1, ref_type='reputation', ref_id='x.com')

        debits = TrialUsageLog.objects.filter(
            user_id=self.user.id, service='reputation', entry_type='debit')
        self.assertEqual(debits.count(), 1)
        self.assertEqual(debits.first().balance_before, 2)  # limit 2, nothing spent yet
        self.assertEqual(debits.first().balance_after, 1)
        self.assertEqual(debits.first().amount, -1)

    # ── lock-order regression guard ───────────────────────────────────────

    def test_pre_locking_service_credit_first_does_not_deadlock(self):
        """Mirrors reputation.py/so_email_accounts.py's own pattern: lock
        ServiceCredit before calling deduct_service_credits(). ServiceTrial
        must lock second, never before, or this would deadlock against a
        caller that only ever goes through deduct_service_credits directly."""
        from django.db import transaction
        with transaction.atomic():
            ServiceCredit.objects.get_or_create(user_id=self.user.id, service='reputation')
            list(ServiceCredit.objects.select_for_update().filter(
                user_id=self.user.id, service='reputation'))
            deduct_service_credits(self.user.id, 'reputation', 2, ref_type='reputation')  # full limit
        self.assertEqual(get_trial_remaining(self.user.id, 'reputation'), 0)


class TrialExpiryTests(TestCase):

    def test_expired_trial_reports_zero_remaining_even_with_unused_allowance(self):
        user = make_user('trial_expired@example.com')
        activate_trial(user)
        _backdate_trial(user)

        for service in SERVICE_KEYS:
            self.assertEqual(get_trial_remaining(user.id, service), 0)
        # The underlying ServiceTrial rows are untouched -- the allowance
        # itself isn't zeroed, access is just gated live off trial_ends_at.
        row = ServiceTrial.objects.get(user_id=user.id, service='email_validation')
        self.assertEqual(row.used, 0)
        self.assertEqual(row.limit, 100)

    def test_deduct_raises_once_expired_rather_than_spending_trial(self):
        user = make_user('trial_expired_spend@example.com')
        activate_trial(user)
        _backdate_trial(user)

        with self.assertRaises(InsufficientCredits) as ctx:
            deduct_service_credits(user.id, 'sales_outreach', 1, ref_type='so_account')
        self.assertFalse(ctx.exception.trial_active)

        row = ServiceTrial.objects.get(user_id=user.id, service='sales_outreach')
        self.assertEqual(row.used, 0, "an expired trial must never be spent")

    def test_expired_trial_falls_through_to_a_paid_wallet_if_present(self):
        user = make_user('trial_expired_paid@example.com')
        activate_trial(user)
        _backdate_trial(user)
        ServiceCredit.objects.create(user_id=user.id, service='sales_outreach', balance=1)

        deduct_service_credits(user.id, 'sales_outreach', 1, ref_type='so_account')

        self.assertEqual(
            ServiceCredit.objects.get(user_id=user.id, service='sales_outreach').balance, 0)
        row = ServiceTrial.objects.get(user_id=user.id, service='sales_outreach')
        self.assertEqual(row.used, 0, "expired trial allowance must not be drawn on")


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class PaidPlanNeverResetsTrialTests(TestCase):
    """Cancelling, expiring, or changing a paid subscription must never
    reset or reactivate a used/expired trial."""

    def _expired_sub(self, user, plan='Standard', days=1):
        return SubsPayment.objects.create(
            user=user, subs_plan=plan, plan_status='Active',
            valid_time=now() - timedelta(days=days),
        )

    def test_subscription_expiry_job_leaves_trial_fields_untouched(self):
        user = make_user('trial_sub_interplay@example.com')
        activate_trial(user)
        _backdate_trial(user)
        user.refresh_from_db()
        trial_started, trial_ends = user.trial_started_at, user.trial_ends_at
        sub = self._expired_sub(user)

        subscription_expiry_job()

        sub.refresh_from_db()
        self.assertEqual(sub.plan_status, 'Inactive')
        user.refresh_from_db()
        self.assertEqual(user.trial_started_at, trial_started)
        self.assertEqual(user.trial_ends_at, trial_ends)
        # Still no second trial available.
        self.assertFalse(is_trial_eligible(user))

    def test_an_active_paid_plan_does_not_grant_or_reactivate_a_trial(self):
        user = make_user('trial_sub_no_grant@example.com')
        activate_trial(user)
        _backdate_trial(user)
        SubsPayment.objects.create(
            user=user, subs_plan='Classic', plan_status='Active',
            valid_time=now() + timedelta(days=30),
        )

        self.assertFalse(is_trial_active(user))
        self.assertFalse(is_trial_eligible(user))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class TrialExpiryNotificationJobTests(TestCase):

    def test_notifies_once_and_logs_forfeited_allowance(self):
        user = make_user('trial_notify@example.com')
        activate_trial(user)
        _backdate_trial(user)

        trial_expiry_notification_job()

        user.refresh_from_db()
        self.assertIsNotNone(user.trial_expiry_notified_at)
        expired_logs = TrialUsageLog.objects.filter(user_id=user.id, entry_type='expired')
        self.assertEqual(expired_logs.count(), 7)
        for row in expired_logs:
            self.assertEqual(row.amount, -TRIAL_LIMITS[row.service])

    def test_running_it_twice_notifies_only_once(self):
        user = make_user('trial_notify_once@example.com')
        activate_trial(user)
        _backdate_trial(user)

        trial_expiry_notification_job()
        first_notified_at = UserTable.objects.get(pk=user.pk).trial_expiry_notified_at
        trial_expiry_notification_job()

        self.assertEqual(
            UserTable.objects.get(pk=user.pk).trial_expiry_notified_at, first_notified_at)
        self.assertEqual(
            TrialUsageLog.objects.filter(user_id=user.id, entry_type='expired').count(), 7,
            "a second run must not double the forfeited-allowance log rows")

    def test_a_still_active_trial_is_not_notified(self):
        user = make_user('trial_notify_active@example.com')
        activate_trial(user)

        trial_expiry_notification_job()

        user.refresh_from_db()
        self.assertIsNone(user.trial_expiry_notified_at)


class NoRegressionForNonTrialUsersTests(TestCase):
    """A user who never activated a trial must see byte-for-byte the same
    behaviour as before this feature existed."""

    def test_trial_remaining_is_zero_for_every_service(self):
        user = make_user('never_trialed@example.com')
        for service in SERVICE_KEYS:
            self.assertEqual(get_trial_remaining(user.id, service), 0)

    def test_effective_balance_is_unaffected_by_the_trial_system(self):
        user = make_user('never_trialed_balance@example.com')
        ServiceCredit.objects.create(user_id=user.id, service='email_validation', balance=42)
        self.assertEqual(get_effective_balance(user.id, 'email_validation'), 42)

    def test_deduct_still_raises_insufficient_with_no_trial_and_no_balance(self):
        user = make_user('never_trialed_deduct@example.com')
        with self.assertRaises(InsufficientCredits) as ctx:
            deduct_service_credits(user.id, 'sales_outreach', 1, ref_type='so_account')
        self.assertFalse(ctx.exception.trial_active)
        self.assertFalse(ctx.exception.trial_exhausted)

    def test_legacy_pool_still_works_with_no_trial_present(self):
        user = make_user('never_trialed_legacy@example.com')
        CurrentCredits.objects.create(user_id=user.id, vc_current_credits=10)
        deduct_service_credits(user.id, 'email_validation', 4, ref_type='validation')
        self.assertEqual(
            CurrentCredits.objects.get(user_id=user.id).vc_current_credits, 6)


class DuplicateEmailRejectedTests(TestCase):
    """Confirms pre-existing behaviour (user_email is unique=True) -- not
    new trial-feature logic, but the anti-abuse story depends on it."""

    def test_duplicate_email_signup_is_rejected_at_the_db_level(self):
        make_user('dupe@example.com')
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            UserTable.objects.create_user(
                user_name='Second', user_email='dupe@example.com', password='StrongPass123!')


class HasEverPaidAndCanOfferTrialTests(TestCase):
    """has_ever_paid()/can_offer_trial() -- the "made any payment, even
    without ever trialing" half of the trial-retirement rule (the other
    half, "already activated a trial", is is_trial_eligible(), covered
    elsewhere in this file)."""

    def test_no_payment_no_trial_can_offer_is_true(self):
        user = make_user('cot_fresh@example.com', verified=True)
        self.assertFalse(has_ever_paid(user.id))
        self.assertTrue(can_offer_trial(user))

    def test_a_payment_row_alone_retires_the_offer(self):
        from Email_validate_app.models import Payment
        user = make_user('cot_paid@example.com', verified=True)
        Payment.objects.create(
            user=user, order_id='order_cot_paid', payment_id='pay_cot_paid', amount='9.99')
        self.assertTrue(has_ever_paid(user.id))
        self.assertFalse(can_offer_trial(user))
        # is_trial_eligible() alone is untouched by payment history --
        # only the broader can_offer_trial() considers it.
        self.assertTrue(is_trial_eligible(user))

    def test_a_subs_payment_row_alone_also_retires_the_offer(self):
        user = make_user('cot_subs_paid@example.com', verified=True)
        SubsPayment.objects.create(
            user=user, order_id='order_cot_subs_paid', payment_id='pay_cot_subs_paid',
            amount='19.99')
        self.assertTrue(has_ever_paid(user.id))
        self.assertFalse(can_offer_trial(user))

    def test_a_service_order_alone_does_not_count_as_paid(self):
        """ServiceOrder is a checkout-intent/quote row only -- never a
        confirmed payment -- so it must not retire the trial offer."""
        from Email_validate_app.models import ServiceOrder
        user = make_user('cot_order_only@example.com', verified=True)
        ServiceOrder.objects.create(
            user=user, order_id='order_cot_order_only', cart_json={}, amount_cents=999)
        self.assertFalse(has_ever_paid(user.id))
        self.assertTrue(can_offer_trial(user))

    def test_already_used_trial_retires_the_offer_even_with_no_payment(self):
        user = make_user('cot_used_trial@example.com', verified=True)
        activate_trial(user)
        self.assertFalse(has_ever_paid(user.id))
        self.assertFalse(can_offer_trial(user))
