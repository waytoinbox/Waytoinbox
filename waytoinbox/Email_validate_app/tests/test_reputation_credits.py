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
from Email_validate_app.tests.credit_test_helpers import make_lot


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

    def test_deducts_from_a_purchased_lot(self):
        """Old-credit retirement: funded via the real new-system grant path
        (ServiceCreditLot), not the retired wallet."""
        make_lot(self.user.id, 'reputation', amount=5)

        body = self._add('example.com').json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(self._live().count(), 1)

    # 2 ---------------------------------------------------------------------

    def test_lot_is_consumed_and_legacy_ac_is_never_touched(self):
        """lot reputation = 5, legacy AC = 50 -> reputation 4, AC still 50 --
        legacy is retired and is never drawn from even though it exists and
        has capacity to spare."""
        make_lot(self.user.id, 'reputation', amount=5)
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add('example.com')

        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    # 3 ---------------------------------------------------------------------

    def test_legacy_ac_alone_cannot_fund_a_new_reputation_entry(self):
        """Old-credit retirement: legacy AC = 50, no lot -> the add must
        fail exactly as if there were no credit at all, and the legacy
        pool itself must be left untouched (nothing was spent)."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        body = self._add('example.com').json()

        self.assertEqual(body['status'], 'error')
        self.assertTrue(body['no_credits'])
        self.assertEqual(legacy_ac(self.user.id), 50)
        self.assertEqual(self._live().count(), 0)

    # 4 ---------------------------------------------------------------------

    def test_lot_drains_then_legacy_does_not_take_over(self):
        """The flow spends 1 per request. Once the 2-credit lot is drained,
        legacy AC (10, present and untouched) must NOT take over -- the
        third request must fail exactly like having no credit at all."""
        make_lot(self.user.id, 'reputation', amount=2)
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        self._add('one.com')
        self._add('two.com')
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 0)
        self.assertEqual(legacy_ac(self.user.id), 10)

        third = self._add('three.com').json()
        self.assertEqual(third['status'], 'error')
        self.assertEqual(self._live().count(), 2)
        self.assertEqual(legacy_ac(self.user.id), 10)

    def test_a_single_deduction_never_spans_lot_and_legacy(self):
        """Old-credit retirement: a deduction that exceeds the lot must be
        refused all-or-nothing -- it must NOT split by drawing the
        remainder from legacy AC any more."""
        make_lot(self.user.id, 'reputation', amount=3)
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)

        with self.assertRaises(InsufficientCredits) as ctx:
            deduct_service_credits(self.user.id, 'reputation', 8,
                                   ref_type='reputation', ref_id='bulk',
                                   description='Reputation Analysis')

        self.assertEqual(ctx.exception.available, 3)
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 3)
        self.assertEqual(legacy_ac(self.user.id), 10)

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
        make_lot(self.user.id, 'reputation', amount=5)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add('audit-domain.com')

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'reputation')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'reputation')
        self.assertEqual(entry.ref_id, 'audit-domain.com')
        self.assertEqual(entry.description, 'Reputation Analysis')

    def test_legacy_ac_alone_writes_no_audit_entry_since_nothing_is_spent(self):
        """Old-credit retirement: legacy AC can no longer fund a spend, so
        an add attempt funded only by it must write NO debit audit entry
        at all -- there is nothing to audit because nothing was spent."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._add('legacy-audit.com')

        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id, entry_type='debit').count(), 0)

    # 7 ---------------------------------------------------------------------

    def test_duplicate_domain_is_rejected_and_costs_nothing(self):
        make_lot(self.user.id, 'reputation', amount=5)

        first = self._add('dup.com').json()
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 4)

        second = self._add('dup.com').json()
        self.assertEqual(second['status'], 'warning')
        self.assertIn('already exists', second['message'])
        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 4)
        self.assertEqual(self._live().filter(domain='dup.com').count(), 1)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='reputation').count(), 1)

    # 8 ---------------------------------------------------------------------

    def test_failed_record_creation_does_not_burn_the_credit(self):
        make_lot(self.user.id, 'reputation', amount=5)

        with patch.object(Reputation.objects, 'create',
                          side_effect=RuntimeError('insert exploded')):
            with self.assertRaises(RuntimeError):
                self._add('boom.com')

        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 5)
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
        make_lot(self.user.id, 'reputation', amount=5)

        with patch('Email_validate_app.services.postmaster.fetch_domain_traffic_stats',
                   side_effect=RuntimeError('postmaster down')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.URL, {'domain_input': 'down.com'})

        self.assertEqual(get_effective_balance(self.user.id, 'reputation'), 5)
        self.assertEqual(self._live().count(), 0)

    # 9 ---------------------------------------------------------------------

    def test_legacy_ac_is_never_copied_into_the_service_wallet(self):
        """Old-credit retirement: legacy AC alone can no longer fund the add
        (so the pool is left untouched, not decremented), and critically it
        must never be copied into a ServiceCredit wallet row as a side
        effect of the attempt."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._add('nocopy.com')

        self.assertEqual(legacy_ac(self.user.id), 50)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='reputation').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_vc_and_cc_are_never_touched(self):
        make_lot(self.user.id, 'reputation', amount=5)
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._add('scope.com')

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(row.ac_current_credits, 50)   # the lot covered it

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
        make_lot(self.user.id, 'reputation', amount=5)

        body = self._add('https://Stripped.com/some/path?q=1').json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['domain'], 'stripped.com')
        self.assertTrue(self._live().filter(domain='stripped.com').exists())

    def test_verified_status_when_postmaster_returns_stats(self):
        make_lot(self.user.id, 'reputation', amount=5)

        body = self._add('verified.com', stats=[{
            'date': '20260101', 'spam_rate': 0.01,
            'domain_reputation': 'HIGH', 'ip_reputation': [],
            'delivery_errors': [],
        }]).json()

        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['rep_status'], 'verified')

    def test_unverified_status_when_postmaster_returns_nothing(self):
        make_lot(self.user.id, 'reputation', amount=5)
        body = self._add('unverified.com').json()
        self.assertEqual(body['rep_status'], 'unverified')

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'domain_input': 'x.com'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Reputation.objects.filter(domain='x.com').count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcPoolTests(TestCase):
    """Old-credit retirement: the legacy AC pool used to be ONE shared balance
    across the four analysis services (reputation, header_analysis,
    ip_blocklist, domain_blocklist). It still exists in the DB for historical
    rows, but it no longer funds any of the four -- each service now spends
    only from its own private ServiceCreditLot, and legacy AC is never drawn
    from or split between them any more.
    """

    def test_legacy_shared_ac_pool_no_longer_funds_any_of_the_four_analysis_services(self):
        user = make_user('rep_shared_ac@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        # None of the four can spend from legacy AC any more.
        for service, ref_type, ref_id, description in (
            ('reputation', 'reputation', 'd', 'Reputation Analysis'),
            ('header_analysis', 'ip_check', 'h', 'Header'),
            ('ip_blocklist', 'ip_check', 'i', 'IP'),
            ('domain_blocklist', 'ip_check', 'd', 'Domain'),
        ):
            with self.assertRaises(InsufficientCredits,
                                   msg=f"{service} must not be fundable by legacy AC"):
                deduct_service_credits(user.id, service, 25, ref_type=ref_type,
                                       ref_id=ref_id, description=description)

        # Nothing was spent -- the legacy pool is untouched.
        self.assertEqual(legacy_ac(user.id), 100)
        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0)

    def test_a_reputation_lot_does_not_leak_into_the_other_three(self):
        user = make_user('rep_no_leak@example.com')
        make_lot(user.id, 'reputation', amount=40)
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        # Reputation's private lot is all it sees -- legacy AC no longer adds in.
        self.assertEqual(get_effective_balance(user.id, 'reputation'), 40)
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0,
                             f"{service} must not see reputation's private lot "
                             f"or the retired legacy AC pool")

    def test_the_fifth_analysis_spend_is_refused_once_ac_is_gone(self):
        """Old-credit retirement: legacy AC alone (even with capacity to
        spare) cannot fund any of the four analysis services any more -- the
        very first spend attempt fails exactly as if there were no credit,
        and the legacy pool is left untouched throughout."""
        user = make_user('rep_ac_exhausted@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=2)

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'reputation', 1, ref_type='reputation',
                                   ref_id='a', description='Reputation Analysis')
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'header_analysis', 1, ref_type='ip_check',
                                   ref_id='b', description='Header')
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'ip_blocklist', 1, ref_type='ip_check',
                                   ref_id='c', description='IP')

        self.assertEqual(legacy_ac(user.id), 2)
