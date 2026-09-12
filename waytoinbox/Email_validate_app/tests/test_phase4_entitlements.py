"""Targeted Phase 4 tests: ServiceEntitlement lifecycle, purchased-lot and
trial expiry, Sales Outreach suspension gates, Reactivate, and the
Reputation/IP Blocklist/Domain Blocklist entitlement wiring.

Primary regression under test: views/so_email_accounts.py's `action=='add'`
now creates a ServiceEntitlement immediately after deducting 1 Sales
Outreach credit (Phase 4 Finding #1 fix) -- previously it deducted the
credit but never created the entitlement, so a newly created SO account
could never be suspended when its funding lot/trial expired.

Follows this project's established TestCase conventions (session-based
login via session['logged_in'], Django's isolated test database only,
external network/SMTP/IMAP calls mocked at the same boundaries the existing
test files already use -- see test_sales_outreach_credits.py,
test_so_sender_authentication.py, test_so_drip_send.py, test_so_imap_sync.py,
test_reputation_credits.py, test_ip_blocklist_credits.py,
test_domain_blocklist_credits.py, test_trial_system.py, test_warmup_content.py).
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client, override_settings
from django.utils.timezone import now

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, ServiceTrial, TrialUsageLog,
    CreditAuditLog, ServiceCreditLot, ServiceEntitlement,
    SOEmailAccount, SOEmailAccountWarmup, WarmupMessage,
    SOCampaign, SOCampaignContact, SOProspect, SOSequenceStep, SOSequenceVariant,
    SOEmailAccountRotation, SOList, SOListProspect, SOConversation,
    Reputation, BlocklistMonitor, DomainBlocklist,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, grant_credit_lot,
    deduct_service_credits, InsufficientCredits,
)
from Email_validate_app.services.entitlement_manager import (
    resolve_funding, create_entitlement, end_entitlement,
    FundingResolutionError, EntitlementIntegrityError,
)
from Email_validate_app.services.trial_manager import TRIAL_LIMITS, activate_trial
from Email_validate_app.tasks.credit_expiry import (
    expire_credit_lots, expire_trial_entitlements, _expire_one_lot,
)
from Email_validate_app.services import so_drip, so_imap
from Email_validate_app.tasks.so_send_campaign import so_send_campaign_task
from Email_validate_app.tasks.warmup import warmup_send_one


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Phase4 Test', user_email=email, password='StrongPass123!')


def login_client(user):
    client = Client(SERVER_NAME='127.0.0.1')
    session = client.session
    session['logged_in'] = user.user_email
    session.save()
    return client


def make_lot(user_id, service, amount=1, hours_ago_purchased=0):
    """A ServiceCreditLot purchased `hours_ago_purchased` hours ago -- 0 for
    a fresh, active lot; > 720 for one that's already past its 30*24h
    expiry (still status='active' until a sweep processes it, exactly like
    a real unfinalized lot)."""
    purchased_at = now() - timedelta(hours=hours_ago_purchased)
    return grant_credit_lot(
        user_id, service, amount,
        source=ServiceCreditLot.SOURCE_SERVICE_CHECKOUT, purchased_at=purchased_at,
        ref_type='service_purchase', ref_id='t',
    )


SO_URL = '/Sales-Outreach/so-accounts/action/'


def so_add(client, email='a@example.com', password='apppassword'):
    return client.post(SO_URL, data=json.dumps({
        'action': 'add', 'email': email, 'provider': 'google',
        'display_name': 'Test', 'password': password,
    }), content_type='application/json')


def so_reactivate(client, account_id):
    return client.post(SO_URL, data=json.dumps({
        'action': 'reactivate', 'id': account_id,
    }), content_type='application/json')


# ═══════════════════════════════════════════════════════════════════════════
# 1. SO account creation -> entitlement
# ═══════════════════════════════════════════════════════════════════════════

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SOAccountEntitlementCreationTests(TestCase):

    def setUp(self):
        self.user = make_user('so_ent_create@example.com')
        self.client = login_client(self.user)

    def _entitlement_for(self, acc_id):
        return ServiceEntitlement.objects.get(so_account_id=acc_id)

    def test_wallet_funded_add_creates_exactly_one_entitlement(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')

        body = so_add(self.client).json()
        self.assertEqual(body['status'], 'ok')
        acc_id = body['id']

        ents = ServiceEntitlement.objects.filter(so_account_id=acc_id)
        self.assertEqual(ents.count(), 1)
        ent = ents.first()
        self.assertEqual(ent.service, 'sales_outreach')
        self.assertEqual(ent.user_id, self.user.id)
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ACTIVE)
        self.assertEqual(ent.funding_source, ServiceEntitlement.FUNDING_WALLET)
        self.assertIsNone(ent.lot_id)
        self.assertTrue(ent.spend_id)
        self.assertEqual(ent.active_key, f'sales_outreach:so_account:{acc_id}')

    def test_lot_funded_add_creates_entitlement_linked_to_lot(self):
        lot = make_lot(self.user.id, 'sales_outreach', amount=1)

        body = so_add(self.client, email='lotfunded@example.com').json()
        self.assertEqual(body['status'], 'ok')
        acc_id = body['id']

        ent = self._entitlement_for(acc_id)
        self.assertEqual(ent.funding_source, ServiceEntitlement.FUNDING_LOT)
        self.assertEqual(ent.lot_id, lot.id)
        self.assertEqual(ent.expires_at, lot.expires_at)

        lot.refresh_from_db()
        self.assertEqual(lot.quantity_remaining, 0)
        self.assertEqual(lot.quantity_used, 1)

    def test_trial_funded_add_creates_entitlement(self):
        activate_trial(self.user)

        body = so_add(self.client, email='trialfunded@example.com').json()
        self.assertEqual(body['status'], 'ok')
        acc_id = body['id']

        ent = self._entitlement_for(acc_id)
        self.assertEqual(ent.funding_source, ServiceEntitlement.FUNDING_TRIAL)
        self.assertIsNone(ent.lot_id)
        self.user.refresh_from_db()
        self.assertEqual(ent.expires_at, self.user.trial_ends_at)

    def test_no_duplicate_entitlement_on_single_add(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        body = so_add(self.client).json()
        acc_id = body['id']

        self.assertEqual(
            ServiceEntitlement.objects.filter(so_account_id=acc_id).count(), 1)

    def test_new_account_entitlement_status_defaults_active(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        acc_id = so_add(self.client).json()['id']
        acc = SOEmailAccount.objects.get(id=acc_id)
        self.assertEqual(acc.entitlement_status, 'active')


# ═══════════════════════════════════════════════════════════════════════════
# 2. Funding resolution — all four sources + safe failure
# ═══════════════════════════════════════════════════════════════════════════

def mock_postmaster(stats=None):
    return patch('Email_validate_app.services.postmaster.fetch_domain_traffic_stats',
                 return_value=stats or [])


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class FundingResolutionTests(TestCase):

    def setUp(self):
        self.user = make_user('funding_res@example.com')
        self.client = login_client(self.user)

    def test_legacy_pool_funded_reputation_creates_entitlement(self):
        """sales_outreach has no legacy pool, so the legacy-pool funding
        source is proven via Reputation (backed by the shared 'ac' pool)."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=5)

        with mock_postmaster([]):
            body = self.client.post('/Reputation_Analysis/', {'domain_input': 'legacy-fund.com'}).json()
        self.assertEqual(body['status'], 'ok')

        ent = ServiceEntitlement.objects.get(reputation_id=body['rep_id'])
        self.assertEqual(ent.funding_source, ServiceEntitlement.FUNDING_LEGACY_POOL)
        self.assertIsNone(ent.lot_id)
        self.assertIsNone(ent.expires_at)

    def test_resolve_funding_raises_when_no_debit_rows(self):
        with self.assertRaises(FundingResolutionError):
            resolve_funding(self.user.id, 'sales_outreach', 'nonexistent-spend-id')

    def test_resolve_funding_raises_when_ambiguous_debit_rows(self):
        """Manufacture two different-kind debit rows sharing one spend_id --
        a shape deduct_service_credits() itself can never produce for
        count=1 (proven separately), but resolve_funding() must still fail
        loudly rather than arbitrarily pick one, as defense-in-depth."""
        spend_id = uuid.uuid4().hex
        TrialUsageLog.objects.create(
            user_id=self.user.id, service='sales_outreach', entry_type='debit',
            amount=-1, spend_id=spend_id)
        CreditAuditLog.objects.create(
            user_id=self.user.id, credit_type='sales_outreach', entry_type='debit',
            amount=-1, service='sales_outreach', spend_id=spend_id)

        with self.assertRaises(FundingResolutionError):
            resolve_funding(self.user.id, 'sales_outreach', spend_id)

    def test_ambiguous_funding_rolls_back_so_account_creation(self):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.services.entitlement_manager.resolve_funding',
                   side_effect=FundingResolutionError('ambiguous')):
            with self.assertRaises(FundingResolutionError):
                so_add(self.client, email='rollback@example.com')

        # Everything the transaction touched must be rolled back together.
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)
        self.assertEqual(
            SOEmailAccount.objects.filter(email='rollback@example.com').count(), 0)
        self.assertEqual(ServiceEntitlement.objects.count(), 0)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Purchased lot expiry
# ═══════════════════════════════════════════════════════════════════════════

class LotExpiryTests(TestCase):

    def setUp(self):
        self.user = make_user('lot_expiry@example.com')

    def test_expired_lot_is_finalized_and_audited(self):
        lot = make_lot(self.user.id, 'email_validation', amount=10, hours_ago_purchased=800)

        expire_credit_lots()

        lot.refresh_from_db()
        self.assertEqual(lot.status, ServiceCreditLot.STATUS_EXPIRED)
        self.assertEqual(lot.quantity_remaining, 0)
        self.assertEqual(lot.quantity_expired, 10)
        self.assertIsNotNone(lot.expired_at)
        # invariant
        self.assertEqual(
            lot.quantity_purchased,
            lot.quantity_remaining + lot.quantity_used + lot.quantity_expired + lot.quantity_revoked)

        entry = CreditAuditLog.objects.get(lot=lot, entry_type='expired')
        self.assertEqual(entry.amount, -10)
        self.assertEqual(entry.user_id, self.user.id)

    def test_zero_remaining_expired_lot_writes_no_audit_row(self):
        # Spend it in full WHILE still active, then advance it into expiry --
        # an already-expired lot can never fund a deduction in the first
        # place (proven separately by test_expired_lot_cannot_fund_new_deduction).
        lot = make_lot(self.user.id, 'email_validation', amount=5, hours_ago_purchased=0)
        deduct_service_credits(self.user.id, 'email_validation', 5, ref_type='validation')
        lot.refresh_from_db()
        self.assertEqual(lot.quantity_remaining, 0)

        ServiceCreditLot.objects.filter(pk=lot.pk).update(expires_at=now() - timedelta(hours=1))

        expire_credit_lots()

        lot.refresh_from_db()
        self.assertEqual(lot.status, ServiceCreditLot.STATUS_EXPIRED)
        self.assertFalse(CreditAuditLog.objects.filter(lot=lot, entry_type='expired').exists())

    def test_future_lot_is_not_expired(self):
        lot = make_lot(self.user.id, 'email_validation', amount=10, hours_ago_purchased=0)

        expire_credit_lots()

        lot.refresh_from_db()
        self.assertEqual(lot.status, ServiceCreditLot.STATUS_ACTIVE)
        self.assertEqual(lot.quantity_remaining, 10)

    def test_repeated_expiry_is_idempotent(self):
        lot = make_lot(self.user.id, 'email_validation', amount=10, hours_ago_purchased=800)

        expire_credit_lots()
        expire_credit_lots()

        lot.refresh_from_db()
        self.assertEqual(lot.quantity_expired, 10)   # not 20
        self.assertEqual(CreditAuditLog.objects.filter(lot=lot, entry_type='expired').count(), 1)

    def test_expired_lot_ends_linked_so_entitlement_and_suspends_account(self):
        client = login_client(self.user)
        # Fund and spend the lot WHILE it's still active (an already-expired
        # lot can never fund a new account -- see the deduction test above),
        # then advance it into expiry before sweeping.
        lot = make_lot(self.user.id, 'sales_outreach', amount=1, hours_ago_purchased=0)
        acc_id = so_add(client, email='lot-suspend@example.com').json()['id']
        ent = ServiceEntitlement.objects.get(so_account_id=acc_id)
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ACTIVE)

        ServiceCreditLot.objects.filter(pk=lot.pk).update(
            purchased_at=now() - timedelta(hours=800), expires_at=now() - timedelta(hours=80))

        expire_credit_lots()

        ent.refresh_from_db()
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ENDED)
        self.assertEqual(ent.end_reason, ServiceEntitlement.END_REASON_EXPIRED)
        self.assertIsNone(ent.active_key)
        acc = SOEmailAccount.objects.get(id=acc_id)
        self.assertEqual(acc.entitlement_status, 'suspended')
        self.assertIsNone(acc.deleted_at)   # suspension must never soft-delete

    def test_expired_lot_cannot_fund_new_deduction(self):
        lot = make_lot(self.user.id, 'email_validation', amount=5, hours_ago_purchased=800)
        expire_credit_lots()

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(self.user.id, 'email_validation', 1, ref_type='validation')

        lot.refresh_from_db()
        self.assertEqual(lot.quantity_remaining, 0)   # untouched by the failed deduction


# ═══════════════════════════════════════════════════════════════════════════
# 4. Trial expiry
# ═══════════════════════════════════════════════════════════════════════════

class TrialExpiryTests(TestCase):

    def setUp(self):
        self.user = make_user('trial_expiry@example.com')
        self.client = login_client(self.user)

    def _backdate_trial(self):
        started = now() - timedelta(days=8)
        self.user.trial_started_at = started
        self.user.trial_ends_at = started + timedelta(days=7)
        self.user.save(update_fields=['trial_started_at', 'trial_ends_at'])

    def test_trial_funded_so_entitlement_ends_after_trial_ends(self):
        activate_trial(self.user)
        acc_id = so_add(self.client, email='trial-expire@example.com').json()['id']
        ent = ServiceEntitlement.objects.get(so_account_id=acc_id)

        self._backdate_trial()
        expire_trial_entitlements()

        ent.refresh_from_db()
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ENDED)
        self.assertEqual(ent.end_reason, ServiceEntitlement.END_REASON_TRIAL_ENDED)
        acc = SOEmailAccount.objects.get(id=acc_id)
        self.assertEqual(acc.entitlement_status, 'suspended')

    def test_future_trial_entitlement_untouched(self):
        activate_trial(self.user)   # trial_ends_at 7 days from now
        acc_id = so_add(self.client, email='trial-future@example.com').json()['id']
        ent = ServiceEntitlement.objects.get(so_account_id=acc_id)

        expire_trial_entitlements()

        ent.refresh_from_db()
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ACTIVE)

    def test_trial_usage_not_modified_by_expiry(self):
        activate_trial(self.user)
        so_add(self.client, email='trial-usage@example.com')
        trial_row = ServiceTrial.objects.get(user_id=self.user.id, service='sales_outreach')
        used_before = trial_row.used
        log_count_before = TrialUsageLog.objects.filter(user_id=self.user.id).count()

        self._backdate_trial()
        expire_trial_entitlements()

        trial_row.refresh_from_db()
        self.assertEqual(trial_row.used, used_before)
        self.assertEqual(TrialUsageLog.objects.filter(user_id=self.user.id).count(), log_count_before)

    def test_expiry_independent_of_notification_flag(self):
        """The Finding-1-adjacent fix from the prior review: entitlement
        ending must not depend on trial_expiry_notified_at at all -- proven
        both when it's already set (as if notified) and when it's NULL."""
        activate_trial(self.user)
        acc_id = so_add(self.client, email='trial-notify-already@example.com').json()['id']
        ent = ServiceEntitlement.objects.get(so_account_id=acc_id)

        self._backdate_trial()
        self.user.trial_expiry_notified_at = now()   # simulate "already notified"
        self.user.save(update_fields=['trial_expiry_notified_at'])

        expire_trial_entitlements()

        ent.refresh_from_db()
        self.assertEqual(ent.status, ServiceEntitlement.STATUS_ENDED)

    def test_repeated_trial_expiry_is_idempotent(self):
        activate_trial(self.user)
        acc_id = so_add(self.client, email='trial-repeat@example.com').json()['id']
        ent = ServiceEntitlement.objects.get(so_account_id=acc_id)
        self._backdate_trial()

        expire_trial_entitlements()
        first_ended_at = ServiceEntitlement.objects.get(pk=ent.pk).ended_at
        expire_trial_entitlements()

        ent.refresh_from_db()
        self.assertEqual(ent.ended_at, first_ended_at)   # untouched by the second run

    def test_multiple_resources_for_same_user_all_end(self):
        activate_trial(self.user)
        acc_id = so_add(self.client, email='trial-multi-so@example.com').json()['id']
        with mock_postmaster([]):
            rep_id = self.client.post('/Reputation_Analysis/',
                                      {'domain_input': 'trial-multi.com'}).json()['rep_id']

        self._backdate_trial()
        expire_trial_entitlements()

        so_ent = ServiceEntitlement.objects.get(so_account_id=acc_id)
        rep_ent = ServiceEntitlement.objects.get(reputation_id=rep_id)
        self.assertEqual(so_ent.status, ServiceEntitlement.STATUS_ENDED)
        self.assertEqual(rep_ent.status, ServiceEntitlement.STATUS_ENDED)
        self.assertEqual(SOEmailAccount.objects.get(id=acc_id).entitlement_status, 'suspended')
        self.assertEqual(Reputation.objects.get(id=rep_id).entitlement_status, 'suspended')


# ═══════════════════════════════════════════════════════════════════════════
# 5. SO suspension gates
# ═══════════════════════════════════════════════════════════════════════════

def make_eligible_account(user, email):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=50, status='connected',
        spf_status='pass', dkim_status='pass', dmarc_status='pass',
    )


class SOSuspensionGateTests(TestCase):

    def setUp(self):
        self.user = make_user('so_suspend_gate@example.com')

    def test_suspended_account_cannot_send_campaign_step(self):
        account = make_eligible_account(self.user, 'suspend-send@example.com')
        account.entitlement_status = 'suspended'
        account.save(update_fields=['entitlement_status'])

        campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Gate Campaign', subject='s', html_body='<p>x</p>',
            status='sending', tracking_enabled=True,
        )
        step = SOSequenceStep.objects.create(campaign=campaign, order=1, wait_days=0, wait_hours=0)
        SOSequenceVariant.objects.create(
            step=step, label='A', subject='Hello', html_body='<p>x</p>', weight=100, is_active=True)
        prospect = SOProspect.objects.create(
            user_id=self.user.id, email='suspend-recipient@example.com', first_name='T', last_name='P',
            status='subscribed')
        cc = SOCampaignContact.objects.create(
            campaign=campaign, prospect=prospect, email=prospect.email,
            account=account, status='sending', current_step=1, attempts=0)

        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            result = so_drip.send_next_step(cc)

        self.assertFalse(result)
        mock_open_smtp.assert_not_called()
        cc.refresh_from_db()
        self.assertIn(cc.status, ('active', 'failed'))

    def test_suspended_account_excluded_from_enrollment(self):
        account = make_eligible_account(self.user, 'suspend-enrol@example.com')
        account.entitlement_status = 'suspended'
        account.save(update_fields=['entitlement_status'])

        campaign = SOCampaign.objects.create(
            user_id=self.user.id, name='Enrol Gate Campaign', subject='s', html_body='<p>x</p>',
            status='sending',
        )
        SOSequenceStep.objects.create(campaign=campaign, order=1, wait_days=0, wait_hours=0)
        SOEmailAccountRotation.objects.create(campaign=campaign, account=account, order=1)
        so_list = SOList.objects.create(user=self.user, name='Gate List')
        campaign.recipient_lists.add(so_list)
        prospect = SOProspect.objects.create(
            user_id=self.user.id, email='enrol-me@example.com', first_name='T', last_name='P',
            status='subscribed')
        SOListProspect.objects.create(so_list=so_list, prospect=prospect)

        result = so_send_campaign_task(campaign.id)

        self.assertEqual(result['status'], 'no_account')
        self.assertFalse(SOCampaignContact.objects.filter(campaign=campaign).exists())

    def test_suspended_account_warmup_send_skipped(self):
        from Email_validate_app.services.warmup import start_warmup

        account = make_eligible_account(self.user, 'suspend-warmup@example.com')
        start_warmup([account.id])
        account.entitlement_status = 'suspended'
        account.save(update_fields=['entitlement_status'])

        message = WarmupMessage.objects.create(
            sender_account=account, sender_email=account.email,
            receiver_email='receiver@example.com',
            identifier=f'WTI-WARMUP-{uuid.uuid4().hex[:12]}',
            scheduled_for=now(),
        )

        with patch('Email_validate_app.services.warmup_sender.open_smtp') as mock_open_smtp:
            result = warmup_send_one(message.id)

        self.assertEqual(result['status'], 'skipped_suspended')
        mock_open_smtp.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.status, 'pending')

    def test_suspended_account_cannot_send_manual_reply(self):
        from Email_validate_app.services.so_inbox import send_reply

        account = make_eligible_account(self.user, 'suspend-reply@example.com')
        account.entitlement_status = 'suspended'
        account.save(update_fields=['entitlement_status'])
        conversation = SOConversation.objects.create(
            thread_key=f'acct:{account.id}:reply-recipient@example.com',
            account=account, email='reply-recipient@example.com', subject='Hi',
        )

        with patch('Email_validate_app.services.so_smtp.open_smtp') as mock_open_smtp:
            with self.assertRaises(ValueError):
                send_reply(conversation, '<p>Hello</p>')
        mock_open_smtp.assert_not_called()

    def test_active_account_regression_still_sends_and_replies(self):
        """Sanity: the new gates must not block a genuinely active account."""
        from Email_validate_app.services.so_inbox import send_reply

        account = make_eligible_account(self.user, 'still-active@example.com')
        conversation = SOConversation.objects.create(
            thread_key=f'acct:{account.id}:still-active-recipient@example.com',
            account=account, email='still-active-recipient@example.com', subject='Hi',
        )
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            msg = send_reply(conversation, '<p>Hello</p>')
        self.assertIsNotNone(msg)

    def test_inbox_sync_still_processes_suspended_account(self):
        """Explicit Phase 4 requirement: inbox/reply sync must keep working
        while an account is suspended."""
        from Email_validate_app.models import SOMessage

        account = make_eligible_account(self.user, 'suspend-sync@example.com')
        account.entitlement_status = 'suspended'
        account.sent_folder = 'Sent'
        account.save(update_fields=['entitlement_status', 'sent_folder'])

        raw = (
            b'From: someone@example.com\r\n'
            b'To: suspend-sync@example.com\r\n'
            b'Subject: Hello\r\n'
            b'Message-ID: <inbound-1@relay.test>\r\n'
            b'\r\nJust checking in.'
        )

        class _FakeIMAP:
            def __init__(self):
                self._store = {b'1': raw}
            def login(self, u, p): return 'OK', [b'Logged in']
            def select(self, mailbox, readonly=True):
                self._store = {b'1': raw} if mailbox == 'INBOX' else {}
                return 'OK', [str(len(self._store)).encode()]
            def search(self, charset, criterion):
                return 'OK', [b' '.join(sorted(self._store.keys()))]
            def fetch(self, num, parts):
                data = self._store.get(num, b'')
                return 'OK', [(b'%s (FETCH {%d}' % (num, len(data)), data), b')']
            def list(self):
                return 'OK', [b'(\\HasNoChildren \\Sent) "/" "Sent"']
            def logout(self): return 'BYE', [b'Logging out']

        with patch('Email_validate_app.services.so_imap.imaplib.IMAP4_SSL', return_value=_FakeIMAP()), \
             patch('Email_validate_app.services.so_smtp.decrypt_password', return_value='x'):
            so_imap.sync_account_inbox(account)

        self.assertTrue(SOMessage.objects.filter(
            account=account, from_email='someone@example.com').exists())


# ═══════════════════════════════════════════════════════════════════════════
# 6. SO Reactivate
# ═══════════════════════════════════════════════════════════════════════════

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SOReactivateTests(TestCase):

    def setUp(self):
        self.user = make_user('so_reactivate@example.com')
        self.client = login_client(self.user)

    def _make_suspended_account(self, email='suspended@example.com'):
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t')
        acc_id = so_add(self.client, email=email).json()['id']
        old_ent = ServiceEntitlement.objects.get(so_account_id=acc_id)
        end_entitlement(old_ent.id, reason=ServiceEntitlement.END_REASON_EXPIRED)
        return SOEmailAccount.objects.get(id=acc_id), old_ent

    def test_successful_reactivate_from_wallet(self):
        acc, old_ent = self._make_suspended_account()
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t2')

        body = so_reactivate(self.client, acc.id).json()

        self.assertEqual(body['status'], 'ok')
        acc.refresh_from_db()
        self.assertEqual(acc.entitlement_status, 'active')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)
        new_ent = ServiceEntitlement.objects.get(so_account_id=acc.id, status='active')
        self.assertEqual(new_ent.funding_source, ServiceEntitlement.FUNDING_WALLET)
        self.assertNotEqual(new_ent.id, old_ent.id)

    def test_successful_reactivate_from_trial(self):
        acc, old_ent = self._make_suspended_account(email='reactivate-trial@example.com')
        activate_trial(self.user)

        body = so_reactivate(self.client, acc.id).json()

        self.assertEqual(body['status'], 'ok')
        new_ent = ServiceEntitlement.objects.get(so_account_id=acc.id, status='active')
        self.assertEqual(new_ent.funding_source, ServiceEntitlement.FUNDING_TRIAL)
        trial_row = ServiceTrial.objects.get(user_id=self.user.id, service='sales_outreach')
        self.assertEqual(trial_row.used, 1)   # one of the 2 trial SO credits spent

    def test_reactivate_with_insufficient_credits_stays_suspended(self):
        acc, old_ent = self._make_suspended_account(email='reactivate-poor@example.com')

        body = so_reactivate(self.client, acc.id).json()

        self.assertEqual(body['status'], 'error')
        acc.refresh_from_db()
        self.assertEqual(acc.entitlement_status, 'suspended')
        self.assertEqual(ServiceEntitlement.objects.filter(so_account_id=acc.id).count(), 1)

    def test_already_active_account_reactivate_is_noop(self):
        add_service_credits(self.user.id, 'sales_outreach', 2,
                            ref_type='service_purchase', ref_id='t')
        acc_id = so_add(self.client, email='already-active@example.com').json()['id']
        balance_before = get_service_balance(self.user.id, 'sales_outreach')

        body = so_reactivate(self.client, acc_id).json()

        self.assertEqual(body['status'], 'ok')
        self.assertIn('already active', body['message'].lower())
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), balance_before)
        self.assertEqual(ServiceEntitlement.objects.filter(so_account_id=acc_id).count(), 1)

    def test_old_entitlement_remains_ended_after_reactivate(self):
        acc, old_ent = self._make_suspended_account(email='old-ended@example.com')
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t2')

        so_reactivate(self.client, acc.id)

        old_ent.refresh_from_db()
        self.assertEqual(old_ent.status, ServiceEntitlement.STATUS_ENDED)
        self.assertIsNone(old_ent.active_key)
        self.assertEqual(
            ServiceEntitlement.objects.filter(so_account_id=acc.id).count(), 2)

    def test_reactivate_failure_rolls_back_everything(self):
        acc, old_ent = self._make_suspended_account(email='reactivate-fail@example.com')
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t2')

        with patch('Email_validate_app.services.entitlement_manager.create_entitlement',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                so_reactivate(self.client, acc.id)

        acc.refresh_from_db()
        self.assertEqual(acc.entitlement_status, 'suspended')
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 1)
        self.assertEqual(ServiceEntitlement.objects.filter(so_account_id=acc.id).count(), 1)

    def test_double_reactivate_does_not_double_charge(self):
        """Sequential double-call proof of the no-op/idempotency logic that
        makes concurrent double-click safe (see the approved lock order:
        ServiceCredit -> SOEmailAccount serializes real concurrent requests
        the same way this sequential call proves the outcome for). A true
        multi-threaded test is out of this targeted pass's scope, matching
        this codebase's existing convention of keeping thread-based
        concurrency tests in their own dedicated files."""
        acc, old_ent = self._make_suspended_account(email='double-reactivate@example.com')
        add_service_credits(self.user.id, 'sales_outreach', 1,
                            ref_type='service_purchase', ref_id='t2')

        first = so_reactivate(self.client, acc.id).json()
        second = so_reactivate(self.client, acc.id).json()

        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['status'], 'ok')
        self.assertIn('already active', second['message'].lower())
        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 0)
        self.assertEqual(
            ServiceEntitlement.objects.filter(so_account_id=acc.id, status='active').count(), 1)

    def test_deleted_account_cannot_reactivate(self):
        acc, old_ent = self._make_suspended_account(email='deleted-reactivate@example.com')
        acc.deleted_at = now()
        acc.save(update_fields=['deleted_at'])

        body = so_reactivate(self.client, acc.id).json()

        self.assertEqual(body['status'], 'error')
        self.assertIn('not found', body['message'].lower())


# ═══════════════════════════════════════════════════════════════════════════
# 7. Reputation / IP Blocklist / Domain Blocklist lifecycle
# ═══════════════════════════════════════════════════════════════════════════

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class ReputationIpDomainLifecycleTests(TestCase):

    def setUp(self):
        self.user = make_user('rid_lifecycle@example.com')
        self.client = login_client(self.user)

    def test_reputation_daily_job_skips_suspended_domain(self):
        from Email_validate_app.tasks.update_reputations import update_all_reputations

        active_rep = Reputation.objects.create(
            user_id=self.user.id, domain='active-rep.com', status='verified')
        suspended_rep = Reputation.objects.create(
            user_id=self.user.id, domain='suspended-rep.com', status='verified',
            entitlement_status='suspended')

        with patch('Email_validate_app.services.postmaster.fetch_domain_traffic_stats',
                   return_value=[]) as mock_fetch:
            update_all_reputations()

        checked_domains = {c.args[0] for c in mock_fetch.call_args_list}
        self.assertIn('active-rep.com', checked_domains)
        self.assertNotIn('suspended-rep.com', checked_domains)

    def test_ip_daily_job_skips_suspended_monitor(self):
        from Email_validate_app.tasks.scheduler_job import scheduler_job

        active_mon = BlocklistMonitor.objects.create(user_id=self.user.id, ips='203.0.113.1')
        BlocklistMonitor.objects.create(
            user_id=self.user.id, ips='203.0.113.2', entitlement_status='suspended')

        with patch('Email_validate_app.tasks.scheduler_job.ip_blacklists',
                   return_value={}) as mock_check:
            scheduler_job()

        checked_ips = {c.args[0] for c in mock_check.call_args_list}
        self.assertIn('203.0.113.1', checked_ips)
        self.assertNotIn('203.0.113.2', checked_ips)

    def test_domain_daily_job_skips_suspended_monitor(self):
        from Email_validate_app.tasks.scheduler_job import my_second_job

        DomainBlocklist.objects.create(user_id=self.user.id, domain='active-dom.com')
        DomainBlocklist.objects.create(
            user_id=self.user.id, domain='suspended-dom.com', entitlement_status='suspended')

        with patch('Email_validate_app.tasks.scheduler_job.domain_blacklists',
                   return_value={}) as mock_check:
            my_second_job()

        checked_domains = {c.args[0] for c in mock_check.call_args_list}
        self.assertIn('active-dom.com', checked_domains)
        self.assertNotIn('suspended-dom.com', checked_domains)

    def test_reputation_remove_readd_creates_fresh_entitlement_and_charges(self):
        add_service_credits(self.user.id, 'reputation', 2,
                            ref_type='service_purchase', ref_id='t')
        with mock_postmaster([]):
            first = self.client.post('/Reputation_Analysis/',
                                     {'domain_input': 'readd.com'}).json()
        old_rep_id = first['rep_id']
        old_ent = ServiceEntitlement.objects.get(reputation_id=old_rep_id)

        # Remove (soft-delete), exactly like the existing "hide" action does.
        Reputation.objects.filter(id=old_rep_id).update(
            is_hidden=True, deleted_at=now())
        balance_before_readd = get_service_balance(self.user.id, 'reputation')

        with mock_postmaster([]):
            second = self.client.post('/Reputation_Analysis/',
                                      {'domain_input': 'readd.com'}).json()

        self.assertEqual(second['status'], 'ok')
        new_rep_id = second['rep_id']
        self.assertNotEqual(new_rep_id, old_rep_id)
        new_ent = ServiceEntitlement.objects.get(reputation_id=new_rep_id)
        self.assertEqual(new_ent.status, ServiceEntitlement.STATUS_ACTIVE)
        self.assertNotEqual(new_ent.id, old_ent.id)
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), balance_before_readd - 1)

    def test_suspended_visible_resource_blocks_readd_without_charging(self):
        """A suspended-but-not-removed resource still trips the existing
        duplicate guard -- no credit is spent trying to 're-add' it, and no
        second entitlement is created."""
        add_service_credits(self.user.id, 'reputation', 2,
                            ref_type='service_purchase', ref_id='t')
        with mock_postmaster([]):
            first = self.client.post('/Reputation_Analysis/',
                                     {'domain_input': 'stuck.com'}).json()
        rep_id = first['rep_id']
        Reputation.objects.filter(id=rep_id).update(entitlement_status='suspended')
        balance_before = get_service_balance(self.user.id, 'reputation')

        with mock_postmaster([]):
            second = self.client.post('/Reputation_Analysis/',
                                      {'domain_input': 'stuck.com'}).json()

        self.assertEqual(second['status'], 'warning')
        self.assertEqual(get_service_balance(self.user.id, 'reputation'), balance_before)
        self.assertEqual(ServiceEntitlement.objects.filter(reputation_id=rep_id).count(), 1)


# ═══════════════════════════════════════════════════════════════════════════
# 8. Existing customer protection
# ═══════════════════════════════════════════════════════════════════════════

class ExistingCustomerProtectionTests(TestCase):

    def setUp(self):
        self.user = make_user('existing_customer@example.com')

    def test_expiry_sweeps_do_not_touch_pre_phase4_resources_or_balances(self):
        # Simulate pre-Phase-4 state: real balances, real resources, but no
        # ServiceEntitlement rows at all (as if created before Phase 4 shipped).
        add_service_credits(self.user.id, 'sales_outreach', 3,
                            ref_type='service_purchase', ref_id='legacy')
        CurrentCredits.objects.create(
            user_id=self.user.id, vc_current_credits=500, ac_current_credits=200, cc_current_credits=50)
        pre_existing_account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Old Sender',
            email='pre-phase4@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='pre-phase4@example.com',
            password='x', daily_limit=50, status='connected',
            spf_status='pass', dkim_status='pass', dmarc_status='pass',
        )
        pre_existing_rep = Reputation.objects.create(
            user_id=self.user.id, domain='pre-phase4.com', status='verified')
        self.assertEqual(ServiceEntitlement.objects.count(), 0)

        expire_credit_lots()
        expire_trial_entitlements()

        self.assertEqual(get_service_balance(self.user.id, 'sales_outreach'), 3)
        cc = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual((cc.vc_current_credits, cc.ac_current_credits, cc.cc_current_credits),
                          (500, 200, 50))
        pre_existing_account.refresh_from_db()
        pre_existing_rep.refresh_from_db()
        self.assertEqual(pre_existing_account.entitlement_status, 'active')
        self.assertEqual(pre_existing_rep.entitlement_status, 'active')
        self.assertIsNone(pre_existing_account.deleted_at)
        # No backfill: still zero entitlement rows anywhere.
        self.assertEqual(ServiceEntitlement.objects.count(), 0)

    def test_trial_expiry_ignores_user_with_no_trial(self):
        SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='X',
            email='no-trial@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='no-trial@example.com',
            password='x', daily_limit=50, status='connected',
        )
        # Must not raise even though trial_ends_at is NULL for this user.
        expire_trial_entitlements()


# ═══════════════════════════════════════════════════════════════════════════
# 9. Trial limits regression
# ═══════════════════════════════════════════════════════════════════════════

class TrialLimitsRegressionTests(TestCase):

    def test_trial_limits_exact_values(self):
        self.assertEqual(TRIAL_LIMITS, {
            'email_validation': 100,
            'email_marketing':  200,
            'sales_outreach':     2,
            'reputation':         2,
            'header_analysis':   25,
            'ip_blocklist':       5,
            'domain_blocklist':   5,
        })
