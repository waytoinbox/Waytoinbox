"""Phase 6, commit 1: Email Validation deduction cutover.

Email Validation now spends from the email_validation service wallet, falling
back to the legacy VC pool, and one email costs exactly one credit. The
behaviour that used to let a zero-balance user validate for free is gone.

Everything here mocks the DNS/MX layer (validate_email_ / get_mx_records_) so
no network call is made and the tests are about credits, not deliverability.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, EmailValidate,
    EmailValidationLog,
)
from Email_validate_app.services.email_validation import core_validate_email
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
)


def make_user(email):
    return UserTable.objects.create_user(
        user_name='EV Test', user_email=email, password='StrongPass123!')


def mock_validation(ok=True):
    """Patch the DNS layer used by core_validate_email."""
    return patch.multiple(
        'Email_validate_app.services.email_validation',
        validate_email_=lambda e: (ok, 'Valid' if ok else 'No MX'),
        get_mx_records_=lambda d: ['mx.example.com'],
    )


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SingleValidationCreditTests(TestCase):

    def setUp(self):
        self.user = make_user('ev_single@example.com')

    # -- new wallet ---------------------------------------------------------

    def test_deducts_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'email_validation', 10,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            result = core_validate_email(self.user.id, 'a@example.com',
                                         deduct_credits=True)

        self.assertIsNone(result.get('error'))
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 9)

    def test_one_email_costs_exactly_one_credit(self):
        add_service_credits(self.user.id, 'email_validation', 5,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            for i in range(3):
                core_validate_email(self.user.id, f'{i}@example.com',
                                    deduct_credits=True)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 2)

    # -- legacy fallback ----------------------------------------------------

    def test_falls_back_to_the_legacy_vc_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=4)
        with mock_validation():
            result = core_validate_email(self.user.id, 'a@example.com',
                                         deduct_credits=True)

        self.assertIsNone(result.get('error'))
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 3)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)

    def test_spends_the_new_wallet_before_the_legacy_pool(self):
        add_service_credits(self.user.id, 'email_validation', 1,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=5)

        with mock_validation():
            core_validate_email(self.user.id, 'a@example.com', deduct_credits=True)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 5)

        # New wallet is empty now, so the next one comes out of legacy.
        with mock_validation():
            core_validate_email(self.user.id, 'b@example.com', deduct_credits=True)
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 4)

    def test_legacy_ac_and_cc_are_never_touched(self):
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=5,
                                      ac_current_credits=50, cc_current_credits=100)
        with mock_validation():
            core_validate_email(self.user.id, 'a@example.com', deduct_credits=True)

        cc = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual((cc.ac_current_credits, cc.cc_current_credits), (50, 100))

    # -- the free behaviour is gone -----------------------------------------

    def test_zero_balance_blocks_validation_entirely(self):
        """THE change: no credit, no validation. Previously this validated for
        free and returned a result."""
        with mock_validation():
            result = core_validate_email(self.user.id, 'a@example.com',
                                         deduct_credits=True)

        self.assertTrue(result.get('need_credits'))
        self.assertIsNotNone(result.get('error'))
        self.assertNotIn('mx_record', result)

    def test_zero_balance_does_no_work_at_all(self):
        """No EmailValidate row, no log row — the work never started."""
        with mock_validation():
            core_validate_email(self.user.id, 'a@example.com', deduct_credits=True)

        self.assertFalse(EmailValidate.objects.filter(user_id=self.user.id).exists())
        self.assertFalse(EmailValidationLog.objects.filter(user_id=self.user.id).exists())

    def test_zero_balance_writes_no_audit_entry(self):
        with mock_validation():
            core_validate_email(self.user.id, 'a@example.com', deduct_credits=True)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)

    def test_exhausting_the_balance_then_blocking(self):
        add_service_credits(self.user.id, 'email_validation', 2,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            first  = core_validate_email(self.user.id, 'a@example.com', deduct_credits=True)
            second = core_validate_email(self.user.id, 'b@example.com', deduct_credits=True)
            third  = core_validate_email(self.user.id, 'c@example.com', deduct_credits=True)

        self.assertIsNone(first.get('error'))
        self.assertIsNone(second.get('error'))
        self.assertTrue(third.get('need_credits'))
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        self.assertEqual(EmailValidate.objects.filter(user_id=self.user.id).count(), 2)

    # -- API callers that do not pay ----------------------------------------

    def test_deduct_credits_false_still_costs_nothing(self):
        """The API path passes deduct_credits=False and is unchanged."""
        add_service_credits(self.user.id, 'email_validation', 3,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            result = core_validate_email(self.user.id, 'a@example.com',
                                         deduct_credits=False)

        self.assertIsNone(result.get('error'))
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 3)

    # -- audit --------------------------------------------------------------

    def test_audit_log_preserves_the_existing_shape(self):
        add_service_credits(self.user.id, 'email_validation', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        with mock_validation():
            core_validate_email(self.user.id, 'target@example.com', deduct_credits=True)

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.ref_type, 'validation')
        self.assertEqual(entry.ref_id, 'target@example.com')
        self.assertEqual(entry.description, 'Single email validation')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.credit_type, 'email_validation')

    def test_legacy_spend_is_audited_against_the_legacy_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=3)
        with mock_validation():
            core_validate_email(self.user.id, 'target@example.com', deduct_credits=True)

        entry = CreditAuditLog.objects.filter(user_id=self.user.id).latest('id')
        self.assertEqual(entry.credit_type, 'vc')
        self.assertEqual(entry.amount, -1)

    # -- failure refunds ----------------------------------------------------

    def test_a_failed_validation_refunds_the_credit(self):
        """The credit is taken before the work, so a crash must give it back."""
        add_service_credits(self.user.id, 'email_validation', 5,
                            ref_type='service_purchase', ref_id='t')

        def boom(email):
            raise RuntimeError('DNS exploded')

        with patch.multiple('Email_validate_app.services.email_validation',
                            validate_email_=boom,
                            get_mx_records_=lambda d: []):
            result = core_validate_email(self.user.id, 'a@example.com',
                                         deduct_credits=True)

        self.assertIsNotNone(result.get('error'))
        self.assertFalse(result.get('need_credits'))
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 5)


URL = '/api/validate/email/'   # the live single-validation endpoint


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SingleValidateEndpointTests(TestCase):
    """The live web endpoint (api.validate_email_view) and its contract.

    Note: views/email_validation.py::single_verify holds the same logic but has
    no URL route at all — it is dead code. It was updated to match, but this is
    the path the site actually calls.
    """

    def setUp(self):
        self.user = make_user('ev_view@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_zero_balance_returns_402_and_validates_nothing(self):
        with mock_validation():
            r = self.client.post(URL, {'email': 'a@example.com'})

        self.assertEqual(r.status_code, 402)
        body = r.json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('credit', body['message'].lower())
        self.assertEqual(body['need'], 1)
        self.assertEqual(body['current'], 0)
        self.assertFalse(EmailValidate.objects.filter(user_id=self.user.id).exists())

    def test_with_credits_it_validates_and_charges_one(self):
        add_service_credits(self.user.id, 'email_validation', 3,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            r = self.client.post(URL, {'email': 'a@example.com'})

        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['email'], 'a@example.com')
        self.assertIn('mx_record', body)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 2)

    def test_reported_credits_reflect_the_new_wallet(self):
        add_service_credits(self.user.id, 'email_validation', 3,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            body = self.client.post(URL, {'email': 'a@example.com'}).json()
        self.assertEqual(body['credits'], 2)

    def test_legacy_vc_user_can_still_validate(self):
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=2)
        with mock_validation():
            r = self.client.post(URL, {'email': 'a@example.com'})

        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 1)

    def test_repeated_submits_each_cost_a_credit(self):
        """No accidental double-charge, and no free retry either."""
        add_service_credits(self.user.id, 'email_validation', 5,
                            ref_type='service_purchase', ref_id='t')
        with mock_validation():
            for _ in range(3):
                self.client.post(URL, {'email': 'same@example.com'})

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 2)
        self.assertEqual(EmailValidate.objects.filter(user_id=self.user.id).count(), 3)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='validation').count(), 3)

    def test_retry_after_a_402_still_costs_nothing(self):
        with mock_validation():
            for _ in range(3):
                r = self.client.post(URL, {'email': 'a@example.com'})
                self.assertEqual(r.status_code, 402)

        self.assertEqual(CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)
        self.assertEqual(EmailValidate.objects.filter(user_id=self.user.id).count(), 0)

    def test_missing_email_is_rejected_before_any_charge(self):
        add_service_credits(self.user.id, 'email_validation', 3,
                            ref_type='service_purchase', ref_id='t')
        r = self.client.post(URL, {'email': ''})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 3)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class BulkValidationCreditTests(TestCase):
    """Bulk behaviour must be unchanged apart from where the credits come from."""

    def setUp(self):
        self.user = make_user('ev_bulk@example.com')

    def test_effective_balance_backs_the_bulk_check(self):
        add_service_credits(self.user.id, 'email_validation', 400,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=350)
        self.assertEqual(get_effective_balance(self.user.id, 'email_validation'), 750)

    def test_bulk_sized_deduction_splits_across_both_pools(self):
        """750 available as 400 new + 350 legacy; a 500-row job takes 400 from
        the new wallet and 100 from legacy."""
        from Email_validate_app.services.credit_manager import deduct_service_credits

        add_service_credits(self.user.id, 'email_validation', 400,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=350)

        deduct_service_credits(self.user.id, 'email_validation', 500,
                               ref_type='validation',
                               description='Bulk validation: WIN_1_2026_01_01')

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 0)
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 250)
        self.assertEqual(get_effective_balance(self.user.id, 'email_validation'), 250)

    def test_insufficient_bulk_balance_deducts_nothing(self):
        """The need_credits branch must leave every balance untouched."""
        from Email_validate_app.services.credit_manager import (
            deduct_service_credits, InsufficientCredits,
        )

        add_service_credits(self.user.id, 'email_validation', 100,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=50)

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'email_validation', 750,
                                   ref_type='validation', description='Bulk')

        self.assertEqual(get_service_balance(self.user.id, 'email_validation'), 100)
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).vc_current_credits, 50)

    def test_bulk_audit_description_is_preserved(self):
        from Email_validate_app.services.credit_manager import deduct_service_credits

        add_service_credits(self.user.id, 'email_validation', 100,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        deduct_service_credits(self.user.id, 'email_validation', 10,
                               ref_type='validation',
                               description='Bulk validation: WIN_1_2026_01_01')

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.ref_type, 'validation')
        self.assertEqual(entry.description, 'Bulk validation: WIN_1_2026_01_01')
        self.assertEqual(entry.amount, -10)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class OtherServicesUntouchedTests(TestCase):
    """This commit changes Email Validation only."""

    def test_no_other_service_wallet_is_created_or_spent(self):
        user = make_user('ev_isolation@example.com')
        add_service_credits(user.id, 'email_validation', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=user.id, vc_current_credits=10,
                                      ac_current_credits=50, cc_current_credits=100)

        with mock_validation():
            core_validate_email(user.id, 'a@example.com', deduct_credits=True)

        self.assertEqual(
            list(ServiceCredit.objects.filter(user_id=user.id)
                 .values_list('service', flat=True)),
            ['email_validation'])
        cc = CurrentCredits.objects.get(user_id=user.id)
        self.assertEqual((cc.ac_current_credits, cc.cc_current_credits), (50, 100))
