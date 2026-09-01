"""Phase 6, commit 2: Email Marketing deduction cutover.

Campaign sends now spend from the email_marketing service wallet, falling back
to the legacy CC pool. The rule that only SUCCESSFUL sends are charged is
unchanged and is the thing most worth pinning down here.

send_campaign_emails() is mocked throughout so no mail is sent and the tests
are about credits, not deliverability.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog,
    Campaign, CampaignEmail, CampaignList,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
)
from Email_validate_app.tasks.send_scheduled_campaigns import send_campaign_emails_task


def make_user(email):
    return UserTable.objects.create_user(
        user_name='EM Test', user_email=email, password='StrongPass123!')


def legacy_cc(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.cc_current_credits or 0) if row else 0


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class EmailMarketingDeductionTests(TestCase):
    """Drives the real task, with only the SMTP layer mocked."""

    def setUp(self):
        self.user = make_user('em_deduct@example.com')
        self.clist = CampaignList.objects.create(
            user=self.user, list_name='Test List')
        self.campaign = Campaign.objects.create(
            user=self.user, campaign_name='Cutover Test',
            campaign_list=self.clist, status='scheduled')

    def _recipients(self, n):
        """Seed n subscribed recipients so the preflight sees a real count."""
        CampaignEmail.objects.bulk_create([
            CampaignEmail(user=self.user, list=self.clist,
                          email=f'r{i}@example.com', subscribed='subscribed')
            for i in range(n)
        ])

    def _run(self, send_count, errors=None):
        """Run the task with send_campaign_emails returning `send_count`.

        _write_campaign_sent_status is patched out because it calls
        close_old_connections() and forces a reconnect — production hardening
        that tears down the TestCase's own transaction. It writes only the
        campaign status, which is not what these tests are about.
        """
        from django.core.cache import cache
        cache.delete(f'send_campaign:{self.campaign.id}')
        with patch('Email_validate_app.services.campaign_sender.send_campaign_emails',
                   return_value=(send_count, errors or [])), \
             patch('Email_validate_app.tasks.send_scheduled_campaigns.'
                   '_write_campaign_sent_status'), \
             patch('Email_validate_app.tasks.sync_campaigns_cloudwatch.sync_campaign_events'):
            return send_campaign_emails_task(self.campaign.id)

    # 1. new wallet ---------------------------------------------------------

    def test_deducts_from_the_new_service_wallet(self):
        self._recipients(10)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')

        result = self._run(send_count=10)

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 90)

    # 2. legacy fallback ----------------------------------------------------

    def test_falls_back_to_the_legacy_cc_pool(self):
        self._recipients(10)
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=50)

        self._run(send_count=10)

        self.assertEqual(legacy_cc(self.user.id), 40)
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 0)

    # 3. new wallet consumed before legacy ----------------------------------

    def test_new_wallet_is_consumed_before_legacy_cc(self):
        """ServiceCredit EM = 800, CC = 300, 500 sends -> EM 300, CC 300."""
        self._recipients(500)
        add_service_credits(self.user.id, 'email_marketing', 800,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=300)

        self._run(send_count=500)

        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 300)
        self.assertEqual(legacy_cc(self.user.id), 300)

    # 4. split across both --------------------------------------------------

    def test_split_between_service_credit_and_legacy_cc(self):
        """ServiceCredit EM = 400, CC = 300, 500 sends -> EM 0, CC 200."""
        self._recipients(500)
        add_service_credits(self.user.id, 'email_marketing', 400,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=300)

        self._run(send_count=500)

        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 0)
        self.assertEqual(legacy_cc(self.user.id), 200)
        self.assertEqual(get_effective_balance(self.user.id, 'email_marketing'), 200)

    # 5 + 6. successful count only ------------------------------------------

    def test_charges_successful_sends_not_attempted(self):
        """Attempted 1000, successful 750, failed 250 -> deduct 750."""
        self._recipients(1000)
        add_service_credits(self.user.id, 'email_marketing', 1000,
                            ref_type='service_purchase', ref_id='t')

        self._run(send_count=750, errors=['failed'] * 250)

        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 250)

    def test_failed_sends_consume_nothing(self):
        self._recipients(100)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')

        self._run(send_count=40, errors=['x'] * 60)

        # Only the 40 that actually went out are charged.
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 60)

    # 7. zero successful ----------------------------------------------------

    def test_zero_successful_sends_deducts_nothing(self):
        self._recipients(50)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=50)

        result = self._run(send_count=0, errors=['all failed'])

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 100)
        self.assertEqual(legacy_cc(self.user.id), 50)
        self.assertFalse(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='campaign').exists())

    # 8. insufficient combined balance --------------------------------------

    def test_insufficient_combined_balance_blocks_the_send(self):
        """Existing behaviour preserved: campaign is failed before sending and
        the reason string is unchanged."""
        self._recipients(100)
        add_service_credits(self.user.id, 'email_marketing', 30,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=20)

        from django.core.cache import cache
        cache.delete(f'send_campaign:{self.campaign.id}')
        with patch('Email_validate_app.services.campaign_sender.send_campaign_emails') as sender:
            result = send_campaign_emails_task(self.campaign.id)

        self.assertEqual(result, {'status': 'failed', 'reason': 'insufficient_cc_credits'})
        sender.assert_not_called()
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, 'failed')
        # Nothing spent.
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 30)
        self.assertEqual(legacy_cc(self.user.id), 20)

    def test_combined_balance_unlocks_a_send_cc_alone_would_block(self):
        """The preflight had to move to the effective balance too: a user whose
        credits are entirely in the new wallet must not be blocked."""
        self._recipients(100)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')
        # Legacy CC is zero — the old CC-only preflight would have failed here.

        result = self._run(send_count=100)

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(get_service_balance(self.user.id, 'email_marketing'), 0)

    # 9. audit --------------------------------------------------------------

    def test_audit_entry_uses_email_marketing(self):
        self._recipients(10)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._run(send_count=10)

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'email_marketing')
        self.assertEqual(entry.amount, -10)
        self.assertEqual(entry.ref_type, 'campaign')
        self.assertEqual(entry.ref_id, str(self.campaign.id))
        self.assertEqual(entry.description, 'Campaign send: Cutover Test')

    def test_split_deduction_audits_both_pools(self):
        self._recipients(500)
        add_service_credits(self.user.id, 'email_marketing', 400,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=300)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._run(send_count=500)

        entries = CreditAuditLog.objects.filter(
            user_id=self.user.id, ref_type='campaign').order_by('id')
        self.assertEqual(
            [(e.credit_type, e.amount) for e in entries],
            [('email_marketing', -400), ('cc', -100)])

    # scope -----------------------------------------------------------------

    def test_vc_and_ac_are_never_touched(self):
        self._recipients(10)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=300)

        self._run(send_count=10)

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.ac_current_credits, 50)
        self.assertEqual(row.cc_current_credits, 300)   # new wallet covered it

    def test_no_legacy_cc_is_migrated_into_the_service_wallet(self):
        """The fallback spends legacy CC in place; it never copies it across."""
        self._recipients(10)
        CurrentCredits.objects.create(user_id=self.user.id, cc_current_credits=500)

        self._run(send_count=10)

        self.assertEqual(legacy_cc(self.user.id), 490)
        # The service wallet was never credited with the legacy balance.
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='email_marketing').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_only_the_email_marketing_wallet_is_created(self):
        self._recipients(10)
        add_service_credits(self.user.id, 'email_marketing', 100,
                            ref_type='service_purchase', ref_id='t')

        self._run(send_count=10)

        self.assertEqual(
            sorted(ServiceCredit.objects.filter(user_id=self.user.id)
                   .values_list('service', flat=True)),
            ['email_marketing'])
