"""Phase 6, commit 8: audit of the remaining legacy credit paths.

Phase 6 moved all seven services onto the ServiceCredit wallets. This file
pins down what that left behind, so the state the audit found cannot silently
regress:

  * the shared legacy AC pool is still ONE balance behind the four analysis
    services, and a private wallet for one of them does not inflate the others;
  * no production view or task calls deduct_ac/cc/vc_credits any more;
  * the legacy grant path (buying an old-style plan) still credits the legacy
    pools, which is deliberate and must keep working.

The grep-based guard at the bottom is the unusual one. It exists because the
expensive mistake in this migration is not a wrong number — it is a deduction
site nobody noticed. If someone reintroduces a legacy deduction in a view or
task, that test fails and names the file.
"""
import pathlib
import re

from django.test import TestCase, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, ServiceCreditLot, CreditAuditLog,
)
from Email_validate_app.services.credit_manager import (
    get_effective_balance,
    deduct_service_credits, InsufficientCredits,
    insert_vc_credits, insert_ac_credits, insert_cc_credits,
)
from Email_validate_app.tests.credit_test_helpers import make_lot

ANALYSIS_SERVICES = ('reputation', 'header_analysis', 'ip_blocklist',
                     'domain_blocklist')

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Audit', user_email=email, password='StrongPass123!')


def legacy(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    if not row:
        return {'vc': 0, 'ac': 0, 'cc': 0}
    return {
        'vc': row.vc_current_credits or 0,
        'ac': row.ac_current_credits or 0,
        'cc': row.cc_current_credits or 0,
    }


# ── Part 3: the shared AC invariant ───────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcInvariantTests(TestCase):
    """Old-credit retirement: the shared legacy AC pool used to fund all four
    analysis services (reputation, header_analysis, ip_blocklist,
    domain_blocklist) as one balance. It still exists in the DB for
    historical rows, but it no longer funds any of the four -- each service
    now spends only from its own private ServiceCreditLot, and legacy AC is
    never drawn from, split, or copied into a wallet for any of them.
    """

    def test_legacy_ac_pool_no_longer_funds_any_of_the_four_services(self):
        """The headline invariant, updated for retirement: a 100-credit
        legacy AC pool funds nothing at all any more -- every one of the
        four services fails identically and the pool stays at 100
        throughout, never partially drained."""
        user = make_user('audit_shared@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ANALYSIS_SERVICES:
            with self.assertRaises(InsufficientCredits,
                                   msg=f"{service} must not be fundable by legacy AC"):
                deduct_service_credits(user.id, service, 25,
                                       ref_type='ip_check', description=service)
            self.assertEqual(legacy(user.id)['ac'], 100,
                             f"legacy AC changed after the refused {service} spend")
            for other in ANALYSIS_SERVICES:
                self.assertEqual(get_effective_balance(user.id, other), 0,
                                 f"{other} disagrees after {service}'s refused spend")

    def test_repeated_analysis_spends_all_fail_since_the_legacy_pool_never_funds_them(self):
        user = make_user('audit_exhausted@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ANALYSIS_SERVICES:
            with self.assertRaises(InsufficientCredits):
                deduct_service_credits(user.id, service, 25,
                                       ref_type='ip_check', description=service)

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'reputation', 1,
                                   ref_type='ip_check', description='one more')

        self.assertEqual(legacy(user.id)['ac'], 100)

    def test_a_private_lot_does_not_inflate_the_other_three(self):
        user = make_user('audit_no_leak@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        for owner in ANALYSIS_SERVICES:
            ServiceCreditLot.objects.filter(user_id=user.id).delete()
            make_lot(user.id, owner, amount=40)

            self.assertEqual(get_effective_balance(user.id, owner), 40)
            for other in ANALYSIS_SERVICES:
                if other == owner:
                    continue
                self.assertEqual(
                    get_effective_balance(user.id, other), 0,
                    f"{other} can see {owner}'s private lot or the retired "
                    f"legacy AC pool")

    def test_the_private_lot_is_always_consumed_and_legacy_ac_is_never_touched(self):
        user = make_user('audit_order@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)
        for service in ANALYSIS_SERVICES:
            make_lot(user.id, service, amount=2)

        for service in ANALYSIS_SERVICES:
            deduct_service_credits(user.id, service, 2,
                                   ref_type='ip_check', description=service)
            self.assertEqual(get_effective_balance(user.id, service), 0)

        # All eight credits came out of the private lots, none out of AC.
        self.assertEqual(legacy(user.id)['ac'], 100)

    def test_a_spend_exceeding_the_lot_is_refused_all_or_nothing_not_split_with_legacy(self):
        """Old-credit retirement: a deduction that exceeds the lot must be
        refused all-or-nothing -- it must NOT split by drawing the
        remainder from the legacy AC pool any more."""
        user = make_user('audit_split@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)
        make_lot(user.id, 'ip_blocklist', amount=30)

        with self.assertRaises(InsufficientCredits) as ctx:
            deduct_service_credits(user.id, 'ip_blocklist', 50,
                                   ref_type='ip_check', description='split')

        self.assertEqual(ctx.exception.available, 30)
        self.assertEqual(get_effective_balance(user.id, 'ip_blocklist'), 30)
        self.assertEqual(legacy(user.id)['ac'], 100)
        # The other three still see nothing -- legacy AC funds none of them.
        for other in ('reputation', 'header_analysis', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, other), 0)

    def test_legacy_ac_is_never_copied_into_a_service_wallet(self):
        user = make_user('audit_nocopy@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ANALYSIS_SERVICES:
            with self.assertRaises(InsufficientCredits):
                deduct_service_credits(user.id, service, 1,
                                       ref_type='ip_check', description=service)

        for row in ServiceCredit.objects.filter(user_id=user.id):
            self.assertEqual(row.balance, 0, f"{row.service} gained a balance")
            self.assertEqual(row.total_purchased, 0,
                             f"{row.service} recorded a purchase it never had")
        self.assertEqual(legacy(user.id)['ac'], 100)

    def test_vc_and_cc_are_untouched_by_analysis_spending(self):
        user = make_user('audit_vc_cc@example.com')
        CurrentCredits.objects.create(user_id=user.id, vc_current_credits=7000,
                                      ac_current_credits=100, cc_current_credits=250)

        for service in ANALYSIS_SERVICES:
            with self.assertRaises(InsufficientCredits):
                deduct_service_credits(user.id, service, 10,
                                       ref_type='ip_check', description=service)

        balances = legacy(user.id)
        self.assertEqual(balances['vc'], 7000)
        self.assertEqual(balances['cc'], 250)
        self.assertEqual(balances['ac'], 100)


# ── Legacy grant path still works ─────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class LegacyGrantPathTests(TestCase):
    """Buying an old-style plan still credits the legacy pools. Phase 6 changed
    where credits are SPENT, never where the legacy ones are granted."""

    def test_legacy_inserters_still_credit_the_legacy_pools(self):
        user = make_user('audit_grant@example.com')

        insert_vc_credits(None, user.id, 1000, ref_type='subscription', ref_id='o1')
        insert_ac_credits(None, user.id, 50, ref_type='subscription', ref_id='o1')
        insert_cc_credits(None, user.id, 200, ref_type='subscription', ref_id='o1')

        self.assertEqual(legacy(user.id), {'vc': 1000, 'ac': 50, 'cc': 200})
        # And nothing leaked into the new wallets.
        self.assertEqual(ServiceCredit.objects.filter(user_id=user.id).count(), 0)

    def test_a_legacy_grant_is_not_spendable_by_the_new_api(self):
        """Old-credit retirement: legacy credits granted through the old
        insert_*_credits path are historical only now -- they do not make a
        service spendable, an attempted deduction fails exactly as if there
        were no credit at all, and the legacy pool is left untouched."""
        user = make_user('audit_grant_spend@example.com')
        insert_ac_credits(None, user.id, 10, ref_type='subscription', ref_id='o1')

        self.assertEqual(get_effective_balance(user.id, 'reputation'), 0)
        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'reputation', 4,
                                   ref_type='ip_check', description='r')
        self.assertEqual(legacy(user.id)['ac'], 10)


# ── The guard: no production code may deduct from the legacy pools ────────────

class NoLegacyDeductionCallersTests(TestCase):
    """Source-level guard. The audit found zero production callers of
    deduct_ac/cc/vc_credits outside credit_manager itself; this keeps it that
    way, and fails with the offending file if someone adds one back."""

    LEGACY_DEDUCTORS = ('deduct_ac_credits', 'deduct_cc_credits',
                        'deduct_vc_credits')

    # Nothing is exempt any more. Commit 8 had to allow credit_manager.py
    # because manage_credits() still called deduct_vc_credits for the
    # bulk-download charge; commit 9 moved that onto the service wallet, so
    # there is now not one call to any of the three anywhere in the app. The
    # regex ignores the `def` lines, so their definitions do not trip it.
    ALLOWED = set()

    def _production_files(self):
        for path in APP_ROOT.rglob('*.py'):
            rel = path.relative_to(APP_ROOT)
            parts = rel.parts
            if 'tests' in parts or 'migrations' in parts or '__pycache__' in parts:
                continue
            yield rel, path

    def test_no_view_or_task_calls_a_legacy_deductor(self):
        offenders = []
        for rel, path in self._production_files():
            if rel in self.ALLOWED:
                continue
            source = path.read_text(encoding='utf-8', errors='ignore')
            for name in self.LEGACY_DEDUCTORS:
                # A call, not an import: `name(` with no `def ` in front.
                for match in re.finditer(rf'(?<!def )\b{name}\s*\(', source):
                    line = source[:match.start()].count('\n') + 1
                    offenders.append(f'{rel.as_posix()}:{line} calls {name}()')

        self.assertEqual(offenders, [], 'Legacy credit deduction reintroduced:\n'
                                        + '\n'.join(offenders))

    def test_the_legacy_deductors_still_exist_for_compatibility(self):
        """They are kept deliberately: removing them would break any
        out-of-tree caller and they are the primitives the fallback in
        deduct_service_credits is modelled on. This asserts the KEEP decision
        was applied, so a later removal is a conscious change."""
        from Email_validate_app.services import credit_manager
        for name in self.LEGACY_DEDUCTORS:
            self.assertTrue(callable(getattr(credit_manager, name, None)),
                            f'{name} disappeared from credit_manager')

    def test_no_production_code_writes_a_legacy_balance_column_directly(self):
        """Balance columns must only be written by credit_manager (the audited,
        locked, audit-logged path) and the admin adjustment tool."""
        pattern = re.compile(
            r'\b(?:vc|ac|cc)_current_credits\s*=\s*(?!models\.)')
        allowed = {
            pathlib.Path('services') / 'credit_manager.py',
            pathlib.Path('models.py'),
            # Admin manual balance adjustment, deliberately direct.
            pathlib.Path('services') / 'admin' / 'user_service.py',
        }

        offenders = []
        for rel, path in self._production_files():
            if rel in allowed:
                continue
            source = path.read_text(encoding='utf-8', errors='ignore')
            for match in pattern.finditer(source):
                line = source[:match.start()].count('\n') + 1
                offenders.append(f'{rel.as_posix()}:{line}')

        self.assertEqual(offenders, [],
                         'Direct legacy balance write outside credit_manager:\n'
                         + '\n'.join(offenders))
