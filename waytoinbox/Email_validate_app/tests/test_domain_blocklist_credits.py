"""Phase 6, commit 7: Domain Blocklist Monitor deduction cutover.

Audit result, same shape as IP Blocklist: every Domain Blocklist charge is for
ADDING a monitor. The names mislead — `check_domain_blocklist`,
`domain_blocklist_check_api` and `description='Domain Blocklist Check'` all
sound like checks, but each creates a DomainBlocklist row and charges only when
one is actually created. The recurring re-scan
(tasks/scheduler_job.my_second_job) charges nothing.

So no check-charging was removed: there was none. What changed is the credit
source (domain_blocklist wallet, then the shared legacy AC pool) and atomicity.

The three ADD paths:
  * views/blocklist.py::check_domain_blocklist    POST /check_domain_blocklist/
  * views/api.py::domain_blocklist_check_api      POST /api/blocklist/domain/
  * views/blocklist.py::add_to_monitors           POST /api/add-to-monitors/  (domain=)

domain_blacklists() is mocked throughout, so no provider network calls happen.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, DomainBlocklist,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
    deduct_service_credits, InsufficientCredits,
)

DOMAIN = 'example-monitor.com'
PROVIDER_RESULT = {'spamhaus': 'Not Listed', 'surbl': 'Listed'}


def make_user(email):
    return UserTable.objects.create_user(
        user_name='DomBL Test', user_email=email, password='StrongPass123!')


def legacy_ac(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.ac_current_credits or 0) if row else 0


def mock_providers(module):
    """Patch domain_blacklists where the given view module bound it."""
    return patch(f'Email_validate_app.views.{module}.domain_blacklists',
                 return_value=dict(PROVIDER_RESULT))


class _Base(TestCase):
    def setUp(self):
        self.user = make_user(f'{self.__class__.__name__.lower()}@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def monitors(self, domain=None):
        qs = DomainBlocklist.objects.filter(user=self.user, is_hidden=False)
        return qs.filter(domain=domain) if domain else qs

    def charges(self):
        """Deduction rows only — add_service_credits also writes a purchase row
        with the same credit_type."""
        return CreditAuditLog.objects.filter(
            user_id=self.user.id, ref_type='ip_check', amount__lt=0)


# ── Path 1: /check_domain_blocklist/ (Domain Blocklist page) ──────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class WebAddPathTests(_Base):

    URL = '/check_domain_blocklist/'

    def _add(self, domain=DOMAIN):
        with mock_providers('blocklist'):
            return self.client.post(self.URL, {'domain_s': domain}, follow=True)

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self._add()

        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 0)

    def test_zero_everywhere_blocks_monitor_creation(self):
        r = self._add()

        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('No Analysis Credits left' in m for m in msgs), msgs)

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self._add()
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

        r = self._add()

        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)
        self.assertEqual(self.charges().count(), 1)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('already exists' in m for m in msgs), msgs)

    def test_repeated_duplicate_adds_never_reduce_either_wallet(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)
        self._add()

        for _ in range(4):
            self._add()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.charges().count(), 1)

    def test_failed_monitor_creation_rolls_the_credit_back(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(DomainBlocklist.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            r = self._add()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)
        self.assertEqual(self.monitors().count(), 0)
        # Existing contract: any other failure still reports "Error: ...".
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Error:' in m for m in msgs), msgs)

    def test_failed_deduction_rolls_the_monitor_back(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.views.blocklist.deduct_service_credits',
                   side_effect=InsufficientCredits('domain_blocklist', 1, 0)):
            r = self._add()

        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('No Analysis Credits left' in m for m in msgs), msgs)

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'domain_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.ref_id, DOMAIN)
        self.assertEqual(entry.description, 'Domain Blocklist Check')

    def test_legacy_spend_is_audited_against_the_shared_ac_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ac')
        self.assertEqual(entry.amount, -1)

    def test_legacy_ac_is_never_copied_into_the_new_wallet(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='domain_blocklist').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_vc_and_cc_are_never_touched(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._add()

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(row.ac_current_credits, 50)

    def test_existing_validation_contracts_are_unchanged(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self.client.post(self.URL, {'domain_s': ''}, follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Domain is required' in m for m in msgs), msgs)

        r = self.client.post(self.URL, {'domain_s': 'not a domain'}, follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('valid domain' in m for m in msgs), msgs)

        r = self.client.get(self.URL, follow=True)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Invalid request method' in m for m in msgs), msgs)

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)
        self.assertEqual(self.monitors().count(), 0)

    def test_protocol_is_still_stripped(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self._add('https://Stripped-Domain.com')

        self.assertTrue(self.monitors('stripped-domain.com').exists())


# ── Path 2: /api/blocklist/domain/ ────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class ApiAddPathTests(_Base):

    URL = '/api/blocklist/domain/'

    def _add(self, domain=DOMAIN):
        with mock_providers('api'):
            return self.client.post(self.URL, {'domain': domain})

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(self.monitors(DOMAIN).count(), 1)

    def test_zero_everywhere_returns_429_and_creates_nothing(self):
        r = self._add()

        self.assertEqual(r.status_code, 429)
        self.assertIn('No Analysis Credits left', r.json()['message'])
        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add()

        r = self._add()

        self.assertEqual(r.json()['status'], 'warning')
        self.assertIn('already being monitored', r.json()['message'])
        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

    def test_failed_monitor_creation_rolls_the_credit_back(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(DomainBlocklist.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            r = self._add()

        self.assertEqual(r.status_code, 500)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)
        self.assertEqual(self.monitors().count(), 0)

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'domain_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.ref_id, DOMAIN)

    def test_existing_contracts_are_unchanged(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self.client.get(self.URL).status_code, 405)
        self.assertEqual(self.client.post(self.URL, {'domain': ''}).status_code, 400)
        self.assertEqual(self.client.post(self.URL, {'domain': 'nope'}).status_code, 400)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'domain': DOMAIN})
        self.assertEqual(r.status_code, 401)


# ── Path 3: /api/add-to-monitors/ (domain= branch) ────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class AddToMonitorsPathTests(_Base):

    URL = '/api/add-to-monitors/'

    def _add(self, domain=DOMAIN):
        with mock_providers('blocklist'):
            return self.client.post(self.URL, {'domain': domain})

    def test_add_deducts_one_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add()

        self.assertEqual(r.json()['domain']['status'], 'added')
        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add()

        self.assertEqual(legacy_ac(self.user.id), 49)

    def test_zero_everywhere_reports_no_credits_and_creates_nothing(self):
        r = self._add()

        self.assertEqual(r.json()['domain']['status'], 'error')
        self.assertIn('No Analysis Credits left', r.json()['domain']['message'])
        self.assertEqual(self.monitors().count(), 0)
        self.assertEqual(self.charges().count(), 0)

    def test_duplicate_add_costs_nothing(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add()

        r = self._add()

        self.assertEqual(r.json()['domain']['status'], 'duplicate')
        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 4)

    def test_audit_entry_uses_the_add_description(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'domain_blocklist')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.description, 'Domain Monitor Add')

    def test_invalid_domain_is_reported_and_costs_nothing(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._add('not a domain')

        self.assertEqual(r.json()['domain']['status'], 'error')
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)


# ── Split deduction and free checks ───────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SplitAndFreeCheckTests(_Base):

    def test_a_single_deduction_can_span_both_pools(self):
        """The add flow spends 1 at a time, so the split is exercised directly
        against deduct_service_credits."""
        add_service_credits(self.user.id, 'domain_blocklist', 3,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        deduct_service_credits(self.user.id, 'domain_blocklist', 8,
                               ref_type='ip_check', ref_id='bulk',
                               description='Domain Monitor Add')

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 0)
        self.assertEqual(legacy_ac(self.user.id), 5)

    def test_new_wallet_drains_then_legacy_ac_takes_over(self):
        add_service_credits(self.user.id, 'domain_blocklist', 2,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        with mock_providers('blocklist'):
            self.client.post('/check_domain_blocklist/', {'domain_s': 'a-one.com'}, follow=True)
            self.client.post('/check_domain_blocklist/', {'domain_s': 'a-two.com'}, follow=True)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 0)
        self.assertEqual(legacy_ac(self.user.id), 10)

        with mock_providers('blocklist'):
            self.client.post('/check_domain_blocklist/', {'domain_s': 'a-three.com'}, follow=True)
        self.assertEqual(legacy_ac(self.user.id), 9)

    def test_the_recurring_rescan_job_charges_nothing(self):
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)
        DomainBlocklist.objects.create(user=self.user, domain=DOMAIN, listed_count=0)
        before = self.charges().count()

        from Email_validate_app.tasks.scheduler_job import my_second_job
        with patch('Email_validate_app.tasks.scheduler_job.domain_blacklists',
                   return_value=dict(PROVIDER_RESULT)):
            my_second_job()

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 5)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.charges().count(), before)

    def test_resubmitting_a_monitored_domain_on_every_path_is_free(self):
        add_service_credits(self.user.id, 'domain_blocklist', 10,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        with mock_providers('blocklist'):
            self.client.post('/check_domain_blocklist/', {'domain_s': DOMAIN}, follow=True)
        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 9)

        with mock_providers('blocklist'):
            self.client.post('/check_domain_blocklist/', {'domain_s': DOMAIN}, follow=True)
            self.client.post('/api/add-to-monitors/', {'domain': DOMAIN})
        with mock_providers('api'):
            self.client.post('/api/blocklist/domain/', {'domain': DOMAIN})

        self.assertEqual(get_service_balance(self.user.id, 'domain_blocklist'), 9)
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self.monitors(DOMAIN).count(), 1)
        self.assertEqual(self.charges().count(), 1)


# ── Shared AC invariant ───────────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcPoolTests(TestCase):
    """With Domain Blocklist cut over, all four analysis services now use the
    new API. The legacy AC pool behind them must still be ONE balance."""

    def test_domain_blocklist_spends_the_same_pool_the_others_see(self):
        user = make_user('dombl_shared@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        deduct_service_credits(user.id, 'domain_blocklist', 25,
                               ref_type='ip_check', description='Domain Monitor Add')

        self.assertEqual(legacy_ac(user.id), 75)
        for service in ('reputation', 'header_analysis', 'ip_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 75,
                             f"{service} did not see Domain Blocklist's spend")

    def test_ac_remains_one_pool_not_four(self):
        user = make_user('dombl_one_pool@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            deduct_service_credits(user.id, service, 25, ref_type='ip_check',
                                   description=service)

        self.assertEqual(legacy_ac(user.id), 0)
        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0)

    def test_a_domain_wallet_does_not_leak_into_the_other_three(self):
        user = make_user('dombl_no_leak@example.com')
        add_service_credits(user.id, 'domain_blocklist', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        self.assertEqual(get_effective_balance(user.id, 'domain_blocklist'), 50)
        for service in ('reputation', 'header_analysis', 'ip_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 10,
                             f"{service} can see Domain Blocklist's private wallet")

    def test_the_fifth_analysis_spend_is_refused_once_ac_is_gone(self):
        user = make_user('dombl_exhausted@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=3)

        for service in ('reputation', 'header_analysis', 'ip_blocklist'):
            deduct_service_credits(user.id, service, 1, ref_type='ip_check',
                                   description=service)

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'domain_blocklist', 1,
                                   ref_type='ip_check', description='Domain')
        self.assertEqual(legacy_ac(user.id), 0)
