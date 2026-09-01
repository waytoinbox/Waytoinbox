"""Tests for Phase 2: subscription expiry must never clear a credit balance.

The expiry job still ends the subscription (SubsPayment.plan_status ->
Inactive) and still notifies the user. What it must no longer do is zero
CurrentCredits.ac/cc — customers keep what they paid for.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils.timezone import now

from Email_validate_app.models import (
    UserTable, CurrentCredits, CreditAuditLog, ServiceCredit, SubsPayment,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, expire_subscription_credits,
)
from Email_validate_app.tasks.scheduler_job import subscription_expiry_job


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Expiry Test', user_email=email, password='StrongPass123!')


# locmem so the job's expiry email never leaves the machine.
@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class ExpiryRetainsCreditsTests(TestCase):

    def _expired_sub(self, user, plan='Standard', days=1):
        return SubsPayment.objects.create(
            user=user, subs_plan=plan, plan_status='Active',
            valid_time=now() - timedelta(days=days),
        )

    def test_expire_clears_no_balance(self):
        user = make_user('expiry_keep@example.com')
        CurrentCredits.objects.create(
            user_id=user.id, ac_current_credits=100,
            cc_current_credits=250, vc_current_credits=7000)
        add_service_credits(user.id, 'reputation', 40,
                            ref_type='service_purchase', ref_id='t1')

        retained = expire_subscription_credits(user.id, self._expired_sub(user))

        cc = CurrentCredits.objects.get(user_id=user.id)
        self.assertEqual(cc.ac_current_credits, 100, "AC was cleared on expiry")
        self.assertEqual(cc.cc_current_credits, 250, "CC was cleared on expiry")
        self.assertEqual(cc.vc_current_credits, 7000, "VC was cleared on expiry")
        self.assertEqual(
            ServiceCredit.objects.get(user_id=user.id, service='reputation').balance, 40)
        self.assertEqual(retained, {'ac': 100, 'cc': 250, 'vc': 7000})

    def test_expire_writes_nothing_to_the_ledger(self):
        """CreditAuditLog records changes. Expiry changes nothing, so it must
        not write an 'expired' row (which previously logged a negative amount)."""
        user = make_user('expiry_ledger@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10,
                                      cc_current_credits=10)
        before = CreditAuditLog.objects.filter(user_id=user.id).count()

        expire_subscription_credits(user.id, self._expired_sub(user, 'Classic'))

        self.assertEqual(CreditAuditLog.objects.filter(user_id=user.id).count(), before)
        self.assertFalse(
            CreditAuditLog.objects.filter(user_id=user.id, entry_type='expired').exists())

    def test_expire_creates_no_credit_row_as_a_side_effect(self):
        """The old implementation used get_or_create and would materialise an
        all-zero CurrentCredits row for users who had none."""
        user = make_user('expiry_norow@example.com')

        retained = expire_subscription_credits(user.id, self._expired_sub(user))

        self.assertEqual(retained, {'ac': 0, 'cc': 0, 'vc': 0})
        self.assertFalse(CurrentCredits.objects.filter(user_id=user.id).exists())

    def test_full_job_deactivates_the_plan_but_keeps_credits(self):
        user = make_user('expiry_job@example.com')
        CurrentCredits.objects.create(
            user_id=user.id, ac_current_credits=100,
            cc_current_credits=250, vc_current_credits=7000)
        sub = self._expired_sub(user, 'Advanced', days=2)

        subscription_expiry_job()

        sub.refresh_from_db()
        self.assertEqual(sub.plan_status, 'Inactive',
                         "expiry job no longer deactivates the plan")
        cc = CurrentCredits.objects.get(user_id=user.id)
        self.assertEqual(
            (cc.ac_current_credits, cc.cc_current_credits, cc.vc_current_credits),
            (100, 250, 7000), "expiry job cleared balances")

    def test_job_leaves_a_still_valid_plan_active(self):
        user = make_user('expiry_active@example.com')
        sub = SubsPayment.objects.create(
            user=user, subs_plan='Standard', plan_status='Active',
            valid_time=now() + timedelta(days=10))

        subscription_expiry_job()

        sub.refresh_from_db()
        self.assertEqual(sub.plan_status, 'Active')

    def test_renewed_plan_accumulates_rather_than_resetting(self):
        """Consequence of retention: leftover credits carry into a new plan,
        because the grant path is additive (balance_before + amount)."""
        from Email_validate_app.services.credit_manager import insert_ac_credits

        user = make_user('expiry_renew@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=30)
        expire_subscription_credits(user.id, self._expired_sub(user))

        insert_ac_credits(None, user.id, 100)

        self.assertEqual(
            CurrentCredits.objects.get(user_id=user.id).ac_current_credits, 130)
