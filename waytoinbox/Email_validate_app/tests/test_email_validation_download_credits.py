"""Phase 6, commit 9: the bulk-result download charge.

This was the last live legacy deduction. manage_credits(), reached from
billing.download_results(), charged VC only. It now charges the
email_validation service wallet, falling back to legacy VC.

The behaviour that must not move is the double-charge guard: a file whose
ListFiles.credite_status is already "Credited" — which bulk validation sets
when it charges at job start — is served without any further charge. The
download charge exists only for older/uncredited files.

manage_credits() is driven directly here rather than through the HTTP view,
because the view's remaining work (Razorpay order creation on the
need_credits branch, file generation) is not what this commit changed.
"""
import re
import pathlib

from django.test import TestCase, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, ListFiles,
    AllEmails,
)
from Email_validate_app.services.credit_manager import (
    manage_credits, add_service_credits, get_service_balance,
    get_effective_balance,
)

TABLE = 'WIN_1_2026_01_01'


def make_user(email):
    return UserTable.objects.create_user(
        user_name='DL Test', user_email=email, password='StrongPass123!')


def legacy_vc(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.vc_current_credits or 0) if row else 0


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class DownloadChargeTests(TestCase):

    def setUp(self):
        self.user = make_user('dl_credits@example.com')
        self.file_entry = ListFiles.objects.create(
            user_id=self.user.id, table_name=TABLE, job_status='Complete')

    def _rows(self, valid=0, invalid=0, pending=0):
        """Seed AllEmails rows. Only Valid/Invalid count toward the charge."""
        objs = []
        for i in range(valid):
            objs.append(AllEmails(user_id=self.user.id,
                                  file_id=self.file_entry.file_id,
                                  email=f'v{i}@example.com',
                                  validation_results='Valid'))
        for i in range(invalid):
            objs.append(AllEmails(user_id=self.user.id,
                                  file_id=self.file_entry.file_id,
                                  email=f'i{i}@example.com',
                                  validation_results='Invalid'))
        for i in range(pending):
            objs.append(AllEmails(user_id=self.user.id,
                                  file_id=self.file_entry.file_id,
                                  email=f'p{i}@example.com',
                                  validation_results=''))
        AllEmails.objects.bulk_create(objs)

    def _download(self, option='all'):
        return manage_credits(option, TABLE, self.user.id, 'Asia/Kolkata')

    def charges(self):
        return CreditAuditLog.objects.filter(
            user_id=self.user.id, ref_type='validation', amount__lt=0)

    # 1, 7, 9 ---------------------------------------------------------------

    def test_new_wallet_pays_the_download_charge(self):
        self._rows(valid=6, invalid=4)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        result = self._download()

        self.assertIsInstance(result, list, "download should return rows")
        self.assertEqual(len(result), 10)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)

    def test_only_validated_rows_are_charged(self):
        """Pending rows are not billable — the count matches the old VC maths."""
        self._rows(valid=3, invalid=2, pending=5)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        self._download()

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 45)

    # 2, 3 ------------------------------------------------------------------

    def test_falls_back_to_legacy_vc(self):
        self._rows(valid=10)
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=50)

        self._download()

        self.assertEqual(legacy_vc(self.user.id), 40)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    def test_new_wallet_is_consumed_before_legacy_vc(self):
        self._rows(valid=10)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=50)

        self._download()

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)
        self.assertEqual(legacy_vc(self.user.id), 50)

    # 4 ---------------------------------------------------------------------

    def test_split_deduction_across_both_pools(self):
        """40 in the new wallet, 30 legacy, a 50-row file -> new 0, legacy 20."""
        self._rows(valid=50)
        add_service_credits(self.user.id, 'email_validation', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=30)

        self._download()

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        self.assertEqual(legacy_vc(self.user.id), 20)
        self.assertEqual(get_effective_balance(self.user.id, 'email_validation'), 20)

    # 5, 13 -----------------------------------------------------------------

    def test_an_already_credited_file_is_never_charged_again(self):
        """The double-charge guard: bulk validation marks the file Credited when
        it charges at job start, so the download must be free."""
        self._rows(valid=10)
        self.file_entry.credite_status = 'Credited'
        self.file_entry.save()
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        result = self._download()

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 10)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 50)
        self.assertEqual(self.charges().count(), 0)

    def test_a_second_download_of_the_same_file_is_free(self):
        self._rows(valid=10)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        self._download()
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)

        for _ in range(3):
            self._download()

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)
        self.assertEqual(self.charges().count(), 1)

    def test_a_successful_charge_marks_the_file_credited(self):
        self._rows(valid=5)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        self._download()

        self.file_entry.refresh_from_db()
        self.assertEqual(self.file_entry.credite_status, 'Credited')

    # 6, 10 -----------------------------------------------------------------

    def test_insufficient_balance_returns_the_row_count_and_charges_nothing(self):
        """Existing contract: manage_credits returns the required count as a
        digit string, which download_results turns into its need_credits
        response."""
        self._rows(valid=10)
        add_service_credits(self.user.id, 'email_validation', 3,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=2)

        result = self._download()

        self.assertEqual(result, '10')
        self.assertTrue(result.isdigit())
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 3)
        self.assertEqual(legacy_vc(self.user.id), 2)
        self.assertEqual(self.charges().count(), 0)

    def test_zero_balance_blocks_the_charge_and_leaves_the_file_uncredited(self):
        self._rows(valid=10)

        result = self._download()

        self.assertEqual(result, '10')
        self.file_entry.refresh_from_db()
        self.assertNotEqual(self.file_entry.credite_status, 'Credited')
        self.assertEqual(self.charges().count(), 0)

    def test_exactly_enough_balance_succeeds(self):
        self._rows(valid=10)
        add_service_credits(self.user.id, 'email_validation', 10,
                            ref_type='service_purchase', ref_id='t')

        result = self._download()

        self.assertIsInstance(result, list)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    # 8 ---------------------------------------------------------------------

    def test_audit_entry_shape(self):
        self._rows(valid=7)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._download()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'email_validation')
        self.assertEqual(entry.amount, -7)
        self.assertEqual(entry.ref_type, 'validation')
        self.assertEqual(entry.description, f'Bulk download: {TABLE}')

    def test_legacy_spend_is_audited_against_the_vc_pool(self):
        self._rows(valid=4)
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=50)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._download()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'vc')
        self.assertEqual(entry.amount, -4)
        self.assertEqual(entry.ref_type, 'validation')

    def test_a_split_charge_audits_both_pools(self):
        self._rows(valid=50)
        add_service_credits(self.user.id, 'email_validation', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=30)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._download()

        entries = CreditAuditLog.objects.filter(
            user_id=self.user.id, ref_type='validation').order_by('id')
        self.assertEqual([(e.credit_type, e.amount) for e in entries],
                         [('email_validation', -40), ('vc', -10)])

    # 11 --------------------------------------------------------------------

    def test_each_file_is_charged_for_its_own_row_count(self):
        second = ListFiles.objects.create(
            user_id=self.user.id, table_name='WIN_2_2026_01_01',
            job_status='Complete')
        self._rows(valid=10)
        AllEmails.objects.bulk_create([
            AllEmails(user_id=self.user.id, file_id=second.file_id,
                      email=f's{i}@example.com',
                      validation_results='Valid') for i in range(4)
        ])
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        self._download()
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)

        manage_credits('all', 'WIN_2_2026_01_01', self.user.id, 'Asia/Kolkata')
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 36)

        self.assertEqual(self.charges().count(), 2)

    # 12 --------------------------------------------------------------------

    def test_no_vc_is_copied_into_the_service_wallet(self):
        self._rows(valid=10)
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=50)

        self._download()

        self.assertEqual(legacy_vc(self.user.id), 40)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='email_validation').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_ac_and_cc_are_never_touched(self):
        self._rows(valid=5)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=100,
                                      ac_current_credits=50, cc_current_credits=200)

        self._download()

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.ac_current_credits, 50)
        self.assertEqual(row.cc_current_credits, 200)
        self.assertEqual(row.vc_current_credits, 100)   # new wallet covered it

    def test_only_the_email_validation_wallet_is_created(self):
        self._rows(valid=5)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        self._download()

        self.assertEqual(
            sorted(ServiceCredit.objects.filter(user_id=self.user.id)
                   .values_list('service', flat=True)),
            ['email_validation'])

    # existing guards preserved ---------------------------------------------

    def test_invalid_table_name_is_still_rejected_without_charging(self):
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        result = manage_credits('all', 'DROP TABLE users', self.user.id, 'Asia/Kolkata')

        self.assertEqual(result, 'Invalid table name or validation error')
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 50)

    def test_another_users_table_is_still_rejected_without_charging(self):
        """The IDOR guard: lookup is scoped to the requesting user."""
        other = make_user('dl_other@example.com')
        add_service_credits(other.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')
        self._rows(valid=10)

        result = manage_credits('all', TABLE, other.id, 'Asia/Kolkata')

        self.assertEqual(result, 'File entry not found in ListFiles.')
        self.assertEqual(get_service_balance(other.id, 'email_validation'), 50)
        self.file_entry.refresh_from_db()
        self.assertNotEqual(self.file_entry.credite_status, 'Credited')

    def test_valid_and_invalid_filters_still_work(self):
        self._rows(valid=6, invalid=4)
        add_service_credits(self.user.id, 'email_validation', 50,
                            ref_type='service_purchase', ref_id='t')

        # First call charges for all 10 validated rows and marks it Credited.
        self.assertEqual(len(self._download('all')), 10)
        self.assertEqual(len(self._download('valid')), 6)
        self.assertEqual(len(self._download('invalid')), 4)
        # Only the first call was billable.
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 40)


class NoLegacyVcDeductionTests(TestCase):
    """Regression guard for this commit specifically."""

    def test_manage_credits_no_longer_calls_deduct_vc_credits(self):
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / 'services' / 'credit_manager.py').read_text(encoding='utf-8')

        body = source[source.index('def manage_credits('):]
        # Stop at the next top-level def so we only inspect manage_credits.
        nxt = re.search(r'\ndef ', body)
        if nxt:
            body = body[:nxt.start()]

        self.assertNotIn('deduct_vc_credits(', body,
                         'manage_credits() still calls the legacy VC deductor')
        self.assertIn("deduct_service_credits(", body)
        self.assertIn("'email_validation'", body)

    def test_deduct_vc_credits_is_kept_for_compatibility(self):
        from Email_validate_app.services import credit_manager
        self.assertTrue(callable(getattr(credit_manager, 'deduct_vc_credits', None)))
