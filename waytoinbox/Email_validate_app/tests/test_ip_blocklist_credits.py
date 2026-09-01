"""Phase 6, commit 6: IP Blocklist Monitor deduction cutover.

Audit result that shaped this commit: every IP Blocklist charge is for ADDING a
monitor. Nothing charges for re-checking an already-monitored IP. The names are
misleading — `check_ip_blacklists`, `ip_blocklist_check_api` and
`description='IP Blocklist Check'` all sound like checks, but each one creates a
BlocklistMonitor and charges only in the non-duplicate branch. The recurring
re-scan (tasks/scheduler_job.scheduler_job) charges nothing at all.

So no check-charging was removed: there was none. What changed is the credit
source (ip_blocklist wallet, then legacy AC) and atomicity.

The three ADD paths:
  * views/blocklist.py::check_ip_blacklists   POST /check_ip_blacklists/
  * views/api.py::ip_blocklist_check_api      POST /api/blocklist/ip/
  * views/blocklist.py::add_to_monitors       POST /api/add-to-monitors/  (ip=)

ip_blacklists() is mocked throughout, so no provider network calls happen.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, BlocklistMonitor,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
    deduct_service_credits, InsufficientCredits,
)

IP = '203.0.113.10'
PROVIDER_RESULT = {'spamhaus': 'Not Listed', 'barracuda': 'Listed'}


def make_user(email):
    return UserTable.objects.create_user(
        user_name='IPBL Test', user_email=email, password='StrongPass123!')


def legacy_ac(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.ac_current_credits or 0) if row else 0


def mock_providers(module):
    """Patch ip_blacklists where the given view module bound it."""
    return patch(f'Email_validate_app.views.{module}.ip_blacklists',
                 return_value=dict(PROVIDER_RESULT))


class _Base(TestCase):
    def setUp(self):
        self.user = make_user(f'{self.__class__.__name__.lower()}@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def monitors(self, ip=IP):
        return BlocklistMonitor.objects.filter(
            user=self.user, ips=ip, is_hidden=False)

    def charges(self):
        """Deduction rows only — add_service_credits also writes a purchase row
        with the same credit_type."""
        return CreditAuditLog.objects.filter(
            user_id=self.user.id, ref_type='ip_check', amount__lt=0)


# ── Path 1: /check_ip_blacklists/ (Blocklist Monitor page) ────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class WebAddPathTests(_Base):

    URL = '/check_ip_blacklists/'

    def _add(self, ip=IP):
        with mock_providers('blocklist'):
            return self.client.post(self.URL, {'Ip_s': ip}, follow=True)

    # 1, 5 ------------------------------------------------------------------

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self._add()

        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

    # 2 ---------------------------------------------------------------------

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    # 3 ---------------------------------------------------------------------

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 0)

    # 4 ---------------------------------------------------------------------

    def test_zero_everywhere_blocks_monitor_creation(self):
        r = self._add()

        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('reached your limit' in m for m in msgs), msgs)

    # 6 ---------------------------------------------------------------------

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self._add()
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

        r = self._add()

        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)
        self.assertEqual(self.charges().count(), 1)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('already being monitored' in m for m in msgs), msgs)

    # 11 --------------------------------------------------------------------

    def test_repeated_duplicate_adds_never_reduce_either_wallet(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)
        self._add()

        for _ in range(4):
            self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.charges().count(), 1)

    # 7 ---------------------------------------------------------------------

    def test_failed_monitor_creation_does_not_burn_a_credit(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(BlocklistMonitor.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            with self.assertRaises(RuntimeError):
                self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)
        self.assertEqual(self.monitors().count(), 0)

    def test_failed_deduction_rolls_the_monitor_back(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.views.blocklist.deduct_service_credits',
                   side_effect=InsufficientCredits('ip_blocklist', 1, 0)):
            r = self._add()

        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('reached your limit' in m for m in msgs), msgs)

    # 8 ---------------------------------------------------------------------

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ip_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.ref_id, IP)
        self.assertEqual(entry.description, 'IP Blocklist Check')

    def test_legacy_spend_is_audited_against_the_shared_ac_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ac')
        self.assertEqual(entry.amount, -1)

    # 9 ---------------------------------------------------------------------

    def test_legacy_ac_is_never_copied_into_the_new_wallet(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='ip_blocklist').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_vc_and_cc_are_never_touched(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._add()

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(row.ac_current_credits, 50)

    # 14 --------------------------------------------------------------------

    def test_existing_validation_contracts_are_unchanged(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self.client.post(self.URL, {'Ip_s': ''}, follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('IP is required' in m for m in msgs), msgs)

        r = self.client.post(self.URL, {'Ip_s': 'not-an-ip'}, follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('valid IP address' in m for m in msgs), msgs)

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)
        self.assertEqual(BlocklistMonitor.objects.filter(user=self.user).count(), 0)


# ── Path 2: /api/blocklist/ip/ ────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class ApiAddPathTests(_Base):

    URL = '/api/blocklist/ip/'

    def _add(self, ip=IP):
        with mock_providers('api'):
            return self.client.post(self.URL, {'ip': ip})

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(self.monitors().count(), 1)

    def test_zero_everywhere_returns_429_and_creates_nothing(self):
        r = self._add()

        self.assertEqual(r.status_code, 429)
        self.assertIn('No Analysis Credits left', r.json()['message'])
        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add()

        r = self._add()

        self.assertEqual(r.json()['status'], 'warning')
        self.assertIn('already being monitored', r.json()['message'])
        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

    def test_failed_monitor_creation_does_not_burn_a_credit(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(BlocklistMonitor.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            with self.assertRaises(RuntimeError):
                self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)
        self.assertEqual(self.monitors().count(), 0)

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ip_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.ref_id, IP)

    def test_existing_contracts_are_unchanged(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self.client.get(self.URL).status_code, 405)
        self.assertEqual(self.client.post(self.URL, {'ip': ''}).status_code, 400)
        self.assertEqual(self.client.post(self.URL, {'ip': 'nope'}).status_code, 400)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'ip': IP})
        self.assertEqual(r.status_code, 401)


# ── Path 3: /api/add-to-monitors/ (ip= branch) ────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class AddToMonitorsPathTests(_Base):

    URL = '/api/add-to-monitors/'

    def _add(self, ip=IP):
        with mock_providers('blocklist'):
            return self.client.post(self.URL, {'ip': ip})

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add()

        self.assertEqual(r.json()['ip']['status'], 'added')
        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)

    def test_zero_everywhere_reports_no_credits_and_creates_nothing(self):
        r = self._add()

        self.assertEqual(r.json()['ip']['status'], 'error')
        self.assertIn('No Analysis Credits left', r.json()['ip']['message'])
        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add()

        r = self._add()

        self.assertEqual(r.json()['ip']['status'], 'duplicate')
        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 4)

    def test_audit_entry_uses_the_add_description(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ip_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.description, 'IP Monitor Add')

    def test_invalid_ip_is_reported_and_costs_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add('not-an-ip')

        self.assertEqual(r.json()['ip']['status'], 'error')
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)


# ── Checks are free ───────────────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class ChecksAreFreeTests(_Base):
    """Requirement 13: charge for adds, not for repeated checks.

    The audit found no check-charging to remove — the recurring re-scan already
    costs nothing, and re-submitting a monitored IP is a duplicate, not a
    billable check. These tests pin that down so a future change cannot
    quietly start charging for it.
    """

    def test_the_recurring_rescan_job_charges_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)
        BlocklistMonitor.objects.create(user=self.user, ips=IP)
        before = self.charges().count()

        from Email_validate_app.tasks.scheduler_job import scheduler_job
        with patch('Email_validate_app.tasks.scheduler_job.ip_blacklists',
                   return_value=dict(PROVIDER_RESULT)):
            scheduler_job()

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 5)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.charges().count(), before)

    def test_rechecking_a_monitored_ip_through_every_path_is_free(self):
        add_service_credits(self.user.id, 'ip_blocklist', 10,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        # One paid add.
        with mock_providers('blocklist'):
            self.client.post('/check_ip_blacklists/', {'Ip_s': IP}, follow=True)
        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 9)

        # Every subsequent submission of the same IP, on every path, is a
        # duplicate and must be free.
        with mock_providers('blocklist'):
            self.client.post('/check_ip_blacklists/', {'Ip_s': IP}, follow=True)
            self.client.post('/api/add-to-monitors/', {'ip': IP})
        with mock_providers('api'):
            self.client.post('/api/blocklist/ip/', {'ip': IP})

        self.assertEqual(get_service_balance(self.user.id, 'ip_blocklist'), 9)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.monitors().count(), 1)
        self.assertEqual(self.charges().count(), 1)


# ── Shared AC invariant ───────────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcPoolTests(TestCase):
    """AC must stay ONE pool across the four analysis services. Reputation,
    Header Analyzer and now IP Blocklist are on their own wallets; Domain
    Blocklist still deducts AC directly."""

    def test_ip_blocklist_spends_the_same_pool_the_others_see(self):
        user = make_user('ipbl_shared@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        deduct_service_credits(user.id, 'ip_blocklist', 25,
                               ref_type='ip_check', description='IP Monitor Add')

        self.assertEqual(legacy_ac(user.id), 75)
        for service in ('reputation', 'header_analysis', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 75,
                             f"{service} did not see IP Blocklist's spend")

    def test_ac_remains_one_pool_not_four(self):
        user = make_user('ipbl_one_pool@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            deduct_service_credits(user.id, service, 25, ref_type='ip_check',
                                   description=service)

        self.assertEqual(legacy_ac(user.id), 0)
        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0)

    def test_an_ip_wallet_does_not_leak_into_the_other_three(self):
        user = make_user('ipbl_no_leak@example.com')
        add_service_credits(user.id, 'ip_blocklist', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        self.assertEqual(get_effective_balance(user.id, 'ip_blocklist'), 50)
        for service in ('reputation', 'header_analysis', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 10,
                             f"{service} can see IP Blocklist's private wallet")
