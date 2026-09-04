"""Tests for the warmup ramp-up redesign: Daily Increment is no longer a
user-typed value — it's always server-computed from Daily Target ÷
Ramp-up Days (services/warmup.py::compute_ramp_increment), with real upper
bounds on Daily Target (40) and Ramp-up Days (30) mirroring the existing
daily_limit-capped-at-120 pattern.

Covers:
  - compute_ramp_increment()'s rounding (ceiling, floored at 1).
  - get_todays_target()'s existing ramp curve is unaffected by switching to
    a server-computed increment.
  - Start Warmup (views/warmup_senders.py::warmup_sender_action) validation
    bounds, and that a client-submitted ramp_up_increment is ignored.
  - Restarting a STOPPED account's warmup now actually persists new
    daily_target/ramp_up_days (previously silently discarded — see
    services/warmup.py::start_warmup's own docstring on the bug).
  - Edit (views/so_email_accounts.py::so_email_account_action) validation
    bounds, partial-update recompute (only one of the two fields changing
    still recomputes using the account's current other value), and that a
    client-submitted ramp_up_increment is ignored there too.
"""
import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils.timezone import now

from Email_validate_app.models import SOEmailAccount, SOEmailAccountWarmup, UserTable
from Email_validate_app.services.warmup import (
    compute_ramp_increment, get_todays_target, start_warmup, update_warmup_settings,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Warmup Ramp Test', user_email=email, password='StrongPass123!')


def make_account(user, email):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Warmup Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=120, status='connected',
    )


class ComputeRampIncrementTests(TestCase):
    """Pure unit tests -- no DB access."""

    def test_exact_division(self):
        self.assertEqual(compute_ramp_increment(40, 10), 4)

    def test_non_exact_division_ceils(self):
        # Today's defaults -- ceiling gives 2, same as the old
        # DEFAULT_RAMP_UP_INCREMENT, so the default experience is unchanged.
        self.assertEqual(compute_ramp_increment(40, 30), 2)

    def test_single_day_ramp_reaches_target_in_one_step(self):
        self.assertEqual(compute_ramp_increment(40, 1), 40)

    def test_small_target_over_many_days_never_stalls_at_zero(self):
        self.assertEqual(compute_ramp_increment(1, 30), 1)

    def test_equal_target_and_days(self):
        self.assertEqual(compute_ramp_increment(40, 40), 1)

    def test_boundary_one_and_one(self):
        self.assertEqual(compute_ramp_increment(1, 1), 1)


class GetTodaysTargetWithComputedIncrementTests(TestCase):
    """get_todays_target()'s existing ramp mechanics are unaffected by
    where ramp_up_increment's value came from."""

    def setUp(self):
        self.user = make_user('warmup_ramp_curve@example.com')
        self.account = make_account(self.user, 'warmup-ramp-curve@example.com')

    def _warmup_on_day(self, days_elapsed, daily_target=40, ramp_up_days=10):
        # Deliberately an unsaved instance, not .objects.create() -- the
        # account has a OneToOneField to SOEmailAccountWarmup, so creating
        # more than one real row for the same account would violate that
        # constraint across this test's several calls. get_todays_target()
        # only ever reads attributes off the object, never queries the DB,
        # so an unsaved instance is exactly as valid a fixture here.
        increment = compute_ramp_increment(daily_target, ramp_up_days)
        started = now() - timedelta(days=days_elapsed)
        return SOEmailAccountWarmup(
            account=self.account, daily_target=daily_target, ramp_up_days=ramp_up_days,
            ramp_up_increment=increment, started_at=started, status='active',
        )

    def test_ramp_curve_matches_expected_progression(self):
        # daily_target=40, ramp_up_days=10 -> increment=4 -> 4,8,12,...,40
        for days_elapsed, expected in ((0, 4), (1, 8), (2, 12), (9, 40)):
            warmup = self._warmup_on_day(days_elapsed)
            self.assertEqual(get_todays_target(warmup), expected,
                             f"day {days_elapsed}")

    def test_stays_flat_at_target_after_ramp_completes(self):
        warmup = self._warmup_on_day(29)  # well past the 10-day ramp window
        self.assertEqual(get_todays_target(warmup), 40)

    def test_never_started_returns_zero(self):
        warmup = SOEmailAccountWarmup(
            account=self.account, daily_target=40, ramp_up_days=10,
            ramp_up_increment=4, started_at=None, status='active',
        )
        self.assertEqual(get_todays_target(warmup), 0)


class StartWarmupValidationTests(TestCase):
    """POST /Warmup/senders/action/ with action='start'."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('start_warmup_validation@example.com')
        self.account = make_account(self.user, 'start-validation@example.com')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _start(self, **overrides):
        payload = {'action': 'start', 'ids': [self.account.id]}
        payload.update(overrides)
        return self.client.post(
            '/Warmup/senders/action/', data=json.dumps(payload),
            content_type='application/json')

    def test_daily_target_above_40_is_rejected(self):
        r = self._start(daily_target=41)
        self.assertEqual(r.json()['errors']['daily_target'], 'Daily target cannot exceed 40.')
        self.assertFalse(SOEmailAccountWarmup.objects.filter(account=self.account).exists())

    def test_ramp_up_days_above_30_is_rejected(self):
        r = self._start(ramp_up_days=31)
        self.assertEqual(r.json()['errors']['ramp_up_days'], 'Ramp-up days cannot exceed 30.')
        self.assertFalse(SOEmailAccountWarmup.objects.filter(account=self.account).exists())

    def test_zero_is_rejected(self):
        r = self._start(daily_target=0)
        self.assertEqual(r.json()['errors']['daily_target'], 'Daily target must be greater than 0.')

    def test_non_numeric_is_rejected(self):
        r = self._start(ramp_up_days='abc')
        self.assertEqual(r.json()['errors']['ramp_up_days'], 'Ramp-up days must be a whole number.')

    def test_valid_values_create_a_row_with_computed_increment(self):
        r = self._start(daily_target=40, ramp_up_days=10)
        self.assertEqual(r.json()['status'], 'ok')
        warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.assertEqual(warmup.daily_target, 40)
        self.assertEqual(warmup.ramp_up_days, 10)
        self.assertEqual(warmup.ramp_up_increment, 4)

    def test_client_submitted_increment_is_ignored(self):
        r = self._start(daily_target=40, ramp_up_days=30, ramp_up_increment=999)
        self.assertEqual(r.json()['status'], 'ok')
        warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.assertEqual(warmup.ramp_up_increment, 2)   # compute_ramp_increment(40, 30), not 999

    def test_missing_values_fall_back_to_service_defaults(self):
        r = self._start()
        self.assertEqual(r.json()['status'], 'ok')
        warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.assertEqual(warmup.daily_target, 40)
        self.assertEqual(warmup.ramp_up_days, 30)
        self.assertEqual(warmup.ramp_up_increment, 2)


class StartWarmupReactivationTests(TestCase):
    """The bug fix: restarting a STOPPED account's warmup with new
    daily_target/ramp_up_days must actually persist them, not silently
    discard them (see services/warmup.py::start_warmup's own docstring)."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('warmup_restart@example.com')
        self.account = make_account(self.user, 'restart@example.com')
        # Enrolled, then stopped -- exactly the state the "Start Warmup"
        # button is shown for again in the UI (see i_SO_Email_Accounts.html's
        # kebab-menu {% else %} branch).
        start_warmup([self.account.id], daily_target=40, ramp_up_days=30)
        self.warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.warmup.status = 'stopped'
        self.warmup.save(update_fields=['status'])

        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_restart_with_new_values_persists_them(self):
        r = self.client.post(
            '/Warmup/senders/action/',
            data=json.dumps({
                'action': 'start', 'ids': [self.account.id],
                'daily_target': 20, 'ramp_up_days': 5,
            }),
            content_type='application/json')
        self.assertEqual(r.json()['status'], 'ok')

        self.warmup.refresh_from_db()
        self.assertEqual(self.warmup.status, 'active')
        self.assertEqual(self.warmup.daily_target, 20)
        self.assertEqual(self.warmup.ramp_up_days, 5)
        self.assertEqual(self.warmup.ramp_up_increment, compute_ramp_increment(20, 5))

    def test_restart_without_new_values_keeps_existing_config(self):
        r = self.client.post(
            '/Warmup/senders/action/',
            data=json.dumps({'action': 'start', 'ids': [self.account.id]}),
            content_type='application/json')
        self.assertEqual(r.json()['status'], 'ok')

        self.warmup.refresh_from_db()
        self.assertEqual(self.warmup.status, 'active')
        self.assertEqual(self.warmup.daily_target, 40)   # untouched
        self.assertEqual(self.warmup.ramp_up_days, 30)   # untouched
        self.assertEqual(self.warmup.ramp_up_increment, 2)


class EditWarmupValidationTests(TestCase):
    """POST /Sales-Outreach/so-accounts/action/ with action='edit', warmup
    sub-payload, on an already-enrolled account."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('edit_warmup_validation@example.com')
        self.account = make_account(self.user, 'edit-validation@example.com')
        start_warmup([self.account.id], daily_target=40, ramp_up_days=30)
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _edit(self, warmup_payload):
        return self.client.post(
            '/Sales-Outreach/so-accounts/action/',
            data=json.dumps({
                'action': 'edit', 'id': self.account.id,
                'daily_limit': self.account.daily_limit,
                'warmup': warmup_payload,
            }),
            content_type='application/json')

    def test_daily_target_above_40_is_rejected(self):
        r = self._edit({'daily_target': 41})
        self.assertEqual(r.json()['errors']['daily_target'], 'Daily target cannot exceed 40.')

    def test_ramp_up_days_above_30_is_rejected(self):
        r = self._edit({'ramp_up_days': 31})
        self.assertEqual(r.json()['errors']['ramp_up_days'], 'Ramp-up days cannot exceed 30.')

    def test_partial_update_recomputes_using_existing_other_value(self):
        """Only ramp_up_days changes -- the recomputed increment must use
        the account's CURRENT daily_target (40), not a stale/default one."""
        r = self._edit({'ramp_up_days': 10})
        self.assertEqual(r.json()['status'], 'ok')
        warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.assertEqual(warmup.daily_target, 40)   # untouched
        self.assertEqual(warmup.ramp_up_days, 10)
        self.assertEqual(warmup.ramp_up_increment, compute_ramp_increment(40, 10))

    def test_client_submitted_increment_is_ignored(self):
        r = self._edit({'daily_target': 40, 'ramp_up_days': 30, 'ramp_up_increment': 999})
        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(r.json()['warmup']['ramp_up_increment'], 2)
        warmup = SOEmailAccountWarmup.objects.get(account=self.account)
        self.assertEqual(warmup.ramp_up_increment, 2)

    def test_response_reflects_the_computed_increment(self):
        r = self._edit({'daily_target': 20, 'ramp_up_days': 5})
        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(r.json()['warmup']['ramp_up_increment'], compute_ramp_increment(20, 5))


class UpdateWarmupSettingsServiceTests(TestCase):
    """Direct service-level coverage of update_warmup_settings()'s
    recompute logic, independent of the view layer's validation."""

    def setUp(self):
        self.user = make_user('update_warmup_service@example.com')
        self.account = make_account(self.user, 'update-service@example.com')
        start_warmup([self.account.id], daily_target=40, ramp_up_days=30)

    def test_updating_only_daily_target_recomputes_with_current_ramp_up_days(self):
        warmup = update_warmup_settings(self.account, daily_target=10)
        self.assertEqual(warmup.daily_target, 10)
        self.assertEqual(warmup.ramp_up_days, 30)   # untouched
        self.assertEqual(warmup.ramp_up_increment, compute_ramp_increment(10, 30))

    def test_no_fields_passed_is_a_true_no_op(self):
        before = SOEmailAccountWarmup.objects.get(account=self.account)
        warmup = update_warmup_settings(self.account)
        self.assertEqual(warmup.daily_target, before.daily_target)
        self.assertEqual(warmup.ramp_up_days, before.ramp_up_days)
        self.assertEqual(warmup.ramp_up_increment, before.ramp_up_increment)

    def test_never_enrolled_account_returns_none(self):
        other_user = make_user('never_enrolled@example.com')
        other_account = make_account(other_user, 'never-enrolled@example.com')
        self.assertIsNone(update_warmup_settings(other_account, daily_target=10))
