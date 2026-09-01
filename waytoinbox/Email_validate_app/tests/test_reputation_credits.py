"""Phase 6, commit 4: Reputation Analysis deduction cutover.

Reputation now spends from the reputation service wallet, falling back to the
legacy AC pool. That AC pool is still ONE balance shared with Header Analyzer
and the two blocklist monitors — the test at the bottom of this file is the one
that matters most, because copying AC into four per-service wallets would
silently quadruple every existing customer's credits.

The Postmaster API is mocked throughout, so no network call is made.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, Reputation,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
    deduct_service_credits, InsufficientCredits,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Rep Test', user_email=email, password='StrongPass123!')


def legacy_ac(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.ac_current_credits or 0) if row else 0


def mock_postmaster(stats=None):
    """Patch the Postmaster lookup the view imports inside the function."""
    return patch('Email_validate_app.services.postmaster.fetch_domain_traffic_stats',
                 return_value=stats or [])


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class ReputationCreditTests(TestCase):

    URL = '/Reputation_Analysis/'

    def setUp(self):
        self.user = make_user('rep_credits@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _add(self, domain='example.com', stats=None):
        with mock_postmaster(stats):
            return self.client.post(self.URL, {'domain_input': domain})

    def _live(self):
        return Reputation.objects.filter(
            user_id=self.user.id, deleted_at__isnull=True)

    # 1 ---------------------------------------------------------------------

    def test_deducts_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._add('example.com').json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(self._live().count(), 1)

    # 2 ---------------------------------------------------------------------

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        """new reputation = 5, legacy AC = 50 -> reputation 4, AC still 50."""
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add('example.com')

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    # 3 ---------------------------------------------------------------------

    def test_falls_back_to_legacy_ac(self):
        """new reputation = 0, legacy AC = 50 -> AC 49."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        body = self._add('example.com').json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 0)

    # 4 ---------------------------------------------------------------------

    def test_new_wallet_drains_then_legacy_ac_takes_over(self):
        """The flow spends 1 per request, so the split is observed across
        consecutive requests rather than within one."""
        add_service_credits(self.user.id, 'reputation', 2,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        self._add('one.com')
        self._add('two.com')
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 0)
        self.assertEqual(legacy_ac(self.user.id), 10)

        self._add('three.com')
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 0)
        self.assertEqual(legacy_ac(self.user.id), 9)

    def test_a_single_deduction_can_span_both_pools(self):
        """deduct_service_credits itself splits when one call needs more than
        the new wallet holds."""
        add_service_credits(self.user.id, 'reputation', 3,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        deduct_service_credits(self.user.id, 'reputation', 8,
                               ref_type='reputation', ref_id='bulk',
                               description='Reputation Analysis')

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 0)
        self.assertEqual(legacy_ac(self.user.id), 5)

    # 5 ---------------------------------------------------------------------

    def test_zero_everywhere_blocks_the_operation(self):
        body = self._add('example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertTrue(body['no_credits'])
        self.assertEqual(body['ac_current_credits'], 0)
        self.assertEqual(self._live().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 0)
        self.assertEqual(legacy_ac(self.user.id), 0)

    def test_zero_balance_writes_no_audit_entry(self):
        self._add('example.com')
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)

    # 6 ---------------------------------------------------------------------

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add('audit-domain.com')

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'reputation')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'reputation')
        self.assertEqual(entry.ref_id, 'audit-domain.com')
        self.assertEqual(entry.description, 'Reputation Analysis')

    def test_legacy_spend_is_audited_against_the_shared_ac_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add('legacy-audit.com')

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ac')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'reputation')

    # 7 ---------------------------------------------------------------------

    def test_duplicate_domain_is_rejected_and_costs_nothing(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        first = self._add('dup.com').json()
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 4)

        second = self._add('dup.com').json()
        self.assertEqual(second['status'], 'warning')
        self.assertIn('already exists', second['message'])
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(self._live().filter(domain='dup.com').count(), 1)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='reputation').count(), 1)

    # 8 ---------------------------------------------------------------------

    def test_failed_record_creation_does_not_burn_the_credit(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch.object(Reputation.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            with self.assertRaises(RuntimeError):
                self._add('boom.com')

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 5)
        self.assertEqual(self._live().count(), 0)

    def test_failed_deduction_rolls_the_record_back(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        # Patch where the view looked the name up: reputation.py imports
        # deduct_service_credits at module level, so the source module's
        # attribute is not what it calls.
        with patch('Email_validate_app.views.reputation.deduct_service_credits',
                   side_effect=InsufficientCredits('reputation', 1, 0)):
            body = self._add('rollback.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertTrue(body['no_credits'])
        self.assertEqual(self._live().count(), 0)
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 5)

    def test_a_postmaster_failure_costs_nothing(self):
        """The lookup now runs before any charge, so a network failure is free.
        Previously the credit had already been taken."""
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.services.postmaster.fetch_domain_traffic_stats',
                   side_effect=RuntimeError('postmaster down')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.URL, {'domain_input': 'down.com'})

        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 5)
        self.assertEqual(self._live().count(), 0)

    # 9 ---------------------------------------------------------------------

    def test_legacy_ac_is_never_copied_into_the_service_wallet(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add('nocopy.com')

        self.assertEqual(legacy_ac(self.user.id), 49)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='reputation').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_vc_and_cc_are_never_touched(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._add('scope.com')

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(row.ac_current_credits, 50)   # new wallet covered it

    def test_only_the_reputation_wallet_is_created(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')
        self._add('only.com')

        self.assertEqual(
            sorted(ServiceCredit.objects.filter(user_id=self.user.id)
                   .values_list('service', flat=True)),
            ['reputation'])

    # 10 --------------------------------------------------------------------

    def test_existing_validation_and_responses_are_unchanged(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        blank = self._add('').json()
        self.assertEqual(blank['status'], 'error')
        self.assertIn('required', blank['message'])

        bad = self._add('not a domain').json()
        self.assertEqual(bad['status'], 'error')
        self.assertIn('valid domain', bad['message'])

        # Nothing charged for a rejected request.
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), 5)

    def test_protocol_is_still_stripped_from_the_domain(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._add('https://Stripped.com/some/path?q=1').json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['domain'], 'stripped.com')
        self.assertTrue(self._live().filter(domain='stripped.com').exists())

    def test_verified_status_when_postmaster_returns_stats(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._add('verified.com', stats=[{
            'date': '20260101', 'spam_rate': 0.01,
            'domain_reputation': 'HIGH', 'ip_reputation': [],
            'delivery_errors': [],
        }]).json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['rep_status'], 'verified')

    def test_unverified_status_when_postmaster_returns_nothing(self):
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')
        body = self._add('unverified.com').json()
        self.assertEqual(body['rep_status'], 'unverified')

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'domain_input': 'x.com'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Reputation.objects.filter(domain='x.com').count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcPoolTests(TestCase):
    """The AC pool must remain ONE balance across the four analysis services.

    Reputation is the first of the four to move to its own wallet; the other
    three still deduct AC directly. Spending across all four must still total
    the one AC balance, never four copies of it.
    """

    def test_ac_remains_one_pool_not_four(self):
        user = make_user('rep_shared_ac@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        # Reputation goes through the new API and falls back to AC.
        deduct_service_credits(user.id, 'reputation', 25,
                               ref_type='reputation', ref_id='d',
                               description='Reputation Analysis')

        # The other three still read the same pool.
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 75,
                             f"{service} did not see reputation's spend")

        deduct_service_credits(user.id, 'header_analysis', 25,
                               ref_type='ip_check', ref_id='h', description='Header')
        deduct_service_credits(user.id, 'ip_blocklist', 25,
                               ref_type='ip_check', ref_id='i', description='IP')
        deduct_service_credits(user.id, 'domain_blocklist', 25,
                               ref_type='ip_check', ref_id='d', description='Domain')

        self.assertEqual(legacy_ac(user.id), 0,
                         "spending 25 on each of four services should exhaust "
                         "one 100-credit pool")
        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0)

    def test_a_reputation_wallet_does_not_leak_into_the_other_three(self):
        user = make_user('rep_no_leak@example.com')
        add_service_credits(user.id, 'reputation', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        self.assertEqual(get_effective_balance(user.id, 'reputation'), 50)
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 10,
                             f"{service} can see reputation's private wallet")

    def test_the_fifth_analysis_spend_is_refused_once_ac_is_gone(self):
        user = make_user('rep_ac_exhausted@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=2)

        deduct_service_credits(user.id, 'reputation', 1, ref_type='reputation',
                               ref_id='a', description='Reputation Analysis')
        deduct_service_credits(user.id, 'header_analysis', 1, ref_type='ip_check',
                               ref_id='b', description='Header')

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'ip_blocklist', 1, ref_type='ip_check',
                                   ref_id='c', description='IP')
        self.assertEqual(legacy_ac(user.id), 0)
