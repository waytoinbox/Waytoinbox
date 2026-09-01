"""Phase 6, commit 5: Header Analyzer deduction cutover.

Header Analyzer now spends from the header_analysis service wallet, falling
back to the legacy AC pool. That AC pool is still ONE balance shared with
Reputation and the two blocklist monitors.

There are two Header Analyzer entry points and both are exercised here:

  * views/api.py::header_analysis_api  (/api/header-analysis/) — the live path;
    the page's form posts here, and API-key clients use it too.
  * views/dmarc.py::Header_Analysis    (/Header_Analysis/) — renders the page on
    GET. Its POST branch is not what the form submits, but it is still reachable
    by a direct POST, and before this commit it had no credit gate at all.

ProfessionalEmailAnalyzer is mocked throughout, so no blacklist lookups or
network calls happen.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog, EmailHeader,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, get_service_balance, get_effective_balance,
    deduct_service_credits, InsufficientCredits,
)

# Passes validate_email_input / _validate_header_input: >= 30 chars and at
# least two of From:/Received:/Subject:/Date:.
HEADER = (
    "Received: from mail.example.com by mx.example.net; Mon, 1 Jan 2026 10:00:00 +0000\n"
    "From: sender@example.com\n"
    "To: recipient@example.net\n"
    "Subject: Test message\n"
    "Date: Mon, 1 Jan 2026 10:00:00 +0000\n"
)

ANALYSIS = {
    'domain_info': {'from_email': 'sender@example.com'},
    'origin_ip': '203.0.113.10',
    'to': 'recipient@example.net',
    'subject': 'Test message',
    'spf': 'pass', 'dkim': 'pass', 'dmarc': 'pass',
    'score': 0, 'risk': 'SAFE',
}


def make_user(email):
    return UserTable.objects.create_user(
        user_name='HA Test', user_email=email, password='StrongPass123!')


def legacy_ac(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    return (row.ac_current_credits or 0) if row else 0


def mock_analyzer(module, result=None, boom=False):
    """Patch ProfessionalEmailAnalyzer where the given view module bound it."""
    target = f'Email_validate_app.views.{module}.ProfessionalEmailAnalyzer'
    if boom:
        return patch(target, side_effect=RuntimeError('analyzer exploded'))
    instance = patch(target)
    return instance


class _AnalyzerPatch:
    """Context manager returning a stubbed analyzer whose analyze() succeeds."""

    def __init__(self, module, result=None):
        self.module = module
        self.result = result if result is not None else dict(ANALYSIS)

    def __enter__(self):
        self._p = patch(f'Email_validate_app.views.{self.module}.ProfessionalEmailAnalyzer')
        cls = self._p.start()
        cls.return_value.analyze.return_value = self.result
        return cls

    def __exit__(self, *exc):
        self._p.stop()
        return False


# ── API path (/api/header-analysis/) ──────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class HeaderAnalyzerApiPathTests(TestCase):

    URL = '/api/header-analysis/'

    def setUp(self):
        self.user = make_user('ha_api@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _post(self, header=HEADER):
        with _AnalyzerPatch('api'):
            return self.client.post(self.URL, {'header': header})

    # 1 ---------------------------------------------------------------------

    def test_deducts_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._post()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 4)

    # 2 ---------------------------------------------------------------------

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        """header_analysis = 5, AC = 50 -> header_analysis 4, AC still 50."""
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._post()

        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    # 3 ---------------------------------------------------------------------

    def test_falls_back_to_legacy_ac(self):
        """header_analysis = 0, AC = 50 -> AC 49."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        r = self._post()

        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(legacy_ac(self.user.id), 49)
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 0)

    # 4 ---------------------------------------------------------------------

    def test_zero_everywhere_blocks_with_429(self):
        r = self._post()

        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('No Analysis Credits left', r.json()['message'])
        self.assertEqual(CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)
        self.assertFalse(EmailHeader.objects.filter(user_id=self.user.id).exists())

    def test_a_new_wallet_alone_is_enough_to_pass_the_gate(self):
        """The gate used to read raw AC, so a user with only new-wallet credits
        was turned away at the door."""
        add_service_credits(self.user.id, 'header_analysis', 1,
                            ref_type='service_purchase', ref_id='t')

        r = self._post()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 0)

    # 5 ---------------------------------------------------------------------

    def test_each_request_costs_exactly_one(self):
        add_service_credits(self.user.id, 'header_analysis', 3,
                            ref_type='service_purchase', ref_id='t')

        for expected in (2, 1, 0):
            self._post()
            self.assertEqual(
                get_service_balance(self.user.id, 'header_analysis'), expected)

        # Fourth request has nothing left.
        self.assertEqual(self._post().status_code, 429)

    def test_reported_credits_reflect_the_new_wallet(self):
        add_service_credits(self.user.id, 'header_analysis', 3,
                            ref_type='service_purchase', ref_id='t')
        self.assertEqual(self._post().json()['credits'], 2)

    # 6 ---------------------------------------------------------------------

    def test_a_failed_analysis_costs_nothing(self):
        """The analyzer runs before the charge, so a crash is free."""
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.views.api.ProfessionalEmailAnalyzer',
                   side_effect=RuntimeError('analyzer exploded')):
            r = self.client.post(self.URL, {'header': HEADER})

        self.assertEqual(r.status_code, 500)
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 5)
        # No deduction row. (The +5 purchase row from add_service_credits also
        # carries credit_type='header_analysis', so filter on the ref_type the
        # deduction uses.)
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='ip_check').count(), 0)

    def test_a_race_lost_after_the_gate_withholds_the_result(self):
        """If the charge fails between the gate and the deduction, the analysis
        must not be served for free."""
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        with _AnalyzerPatch('api'), \
             patch('Email_validate_app.views.api.deduct_service_credits',
                   side_effect=InsufficientCredits('header_analysis', 1, 0)):
            r = self.client.post(self.URL, {'header': HEADER})

        self.assertEqual(r.status_code, 429)
        self.assertNotIn('results', r.json())
        self.assertFalse(EmailHeader.objects.filter(user_id=self.user.id).exists())

    # 7 ---------------------------------------------------------------------

    def test_repeated_identical_headers_each_cost_one(self):
        """Header analysis has no duplicate concept — every run is billable."""
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        for _ in range(3):
            self._post()

        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 2)
        # ref_type filters out the +5 purchase entry, which shares the
        # header_analysis credit_type.
        self.assertEqual(
            CreditAuditLog.objects.filter(user_id=self.user.id,
                                          ref_type='ip_check').count(), 3)

    # 8 ---------------------------------------------------------------------

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._post()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'header_analysis')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.description, 'Header Analysis')

    def test_legacy_spend_is_audited_against_the_shared_ac_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._post()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'ac')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')

    # 9 ---------------------------------------------------------------------

    def test_legacy_ac_is_never_copied_into_the_service_wallet(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._post()

        self.assertEqual(legacy_ac(self.user.id), 49)
        row = ServiceCredit.objects.filter(
            user_id=self.user.id, service='header_analysis').first()
        self.assertTrue(row is None or row.balance == 0)
        self.assertTrue(row is None or row.total_purchased == 0)

    def test_vc_and_cc_are_never_touched(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, vc_current_credits=7000,
                                      ac_current_credits=50, cc_current_credits=100)

        self._post()

        row = CurrentCredits.objects.get(user_id=self.user.id)
        self.assertEqual(row.vc_current_credits, 7000)
        self.assertEqual(row.cc_current_credits, 100)
        self.assertEqual(row.ac_current_credits, 50)

    def test_only_the_header_analysis_wallet_is_created(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        self._post()
        self.assertEqual(
            sorted(ServiceCredit.objects.filter(user_id=self.user.id)
                   .values_list('service', flat=True)),
            ['header_analysis'])

    # 12 --------------------------------------------------------------------

    def test_existing_contracts_are_unchanged(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self.client.get(self.URL).status_code, 405)

        short = self.client.post(self.URL, {'header': 'too short'})
        self.assertEqual(short.status_code, 400)
        self.assertEqual(short.json()['status'], 'error')

        empty = self.client.post(self.URL, {'header': ''})
        self.assertEqual(empty.status_code, 400)

        # None of the rejected requests were charged.
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 5)

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'header': HEADER})
        self.assertEqual(r.status_code, 401)

    def test_successful_response_still_persists_an_emailheader_row(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        self._post()
        self.assertTrue(EmailHeader.objects.filter(user_id=self.user.id).exists())


# ── Web path (/Header_Analysis/) ──────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class HeaderAnalyzerWebPathTests(TestCase):
    """views/dmarc.py::Header_Analysis. Before this commit this POST branch had
    no credit gate and swallowed a failed deduction, so it served free
    analyses."""

    URL = '/Header_Analysis/'

    def setUp(self):
        self.user = make_user('ha_web@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _post(self, header=HEADER):
        with _AnalyzerPatch('dmarc'):
            return self.client.post(self.URL, {'header': header})

    def test_deducts_from_the_new_service_wallet(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self._post()

        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.context['results'])
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 4)

    def test_new_wallet_is_consumed_before_legacy_ac(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._post()

        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 4)
        self.assertEqual(legacy_ac(self.user.id), 50)

    def test_falls_back_to_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        self._post()

        self.assertEqual(legacy_ac(self.user.id), 49)

    def test_zero_everywhere_now_blocks_the_analysis(self):
        """THE bypass this commit closes: with no credits the analyser used to
        run anyway and the failed deduction was swallowed."""
        r = self._post()

        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context['results'])
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('No Analysis Credits left' in m for m in msgs), msgs)
        self.assertEqual(CreditAuditLog.objects.filter(user_id=self.user.id).count(), 0)

    def test_analyzer_is_not_even_run_without_credits(self):
        with patch('Email_validate_app.views.dmarc.ProfessionalEmailAnalyzer') as cls:
            self.client.post(self.URL, {'header': HEADER})
        cls.assert_not_called()

    def test_audit_entry_shape(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        CreditAuditLog.objects.filter(user_id=self.user.id).delete()

        self._post()

        entry = CreditAuditLog.objects.get(user_id=self.user.id)
        self.assertEqual(entry.credit_type, 'header_analysis')
        self.assertEqual(entry.amount, -1)
        self.assertEqual(entry.ref_type, 'ip_check')
        self.assertEqual(entry.description, 'Header Analysis')

    def test_a_failed_analysis_costs_nothing(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        with patch('Email_validate_app.views.dmarc.ProfessionalEmailAnalyzer',
                   side_effect=RuntimeError('analyzer exploded')):
            r = self.client.post(self.URL, {'header': HEADER})

        self.assertIsNone(r.context['results'])
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 5)

    def test_existing_validation_messages_are_unchanged(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self.client.post(self.URL, {'header': 'too short'})
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Input too short' in m for m in msgs), msgs)
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 5)

        r = self.client.post(self.URL, {'header': ''})
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Paste email/header or upload file' in m for m in msgs), msgs)
        self.assertEqual(get_service_balance(self.user.id, 'header_analysis'), 5)

    def test_login_is_still_required(self):
        r = Client(SERVER_NAME='127.0.0.1').post(self.URL, {'header': HEADER})
        self.assertEqual(r.status_code, 302)

    def test_get_still_renders_the_page(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)


# ── Shared AC invariant ───────────────────────────────────────────────────────

@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SharedAcPoolTests(TestCase):
    """AC must stay ONE pool across the four analysis services.

    Reputation and Header Analyzer are now on their own wallets; IP and Domain
    Blocklist still deduct AC directly. All four must keep drawing on the same
    single AC balance.
    """

    def test_header_analyzer_spends_the_same_pool_the_others_see(self):
        user = make_user('ha_shared@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        deduct_service_credits(user.id, 'header_analysis', 25,
                               ref_type='ip_check', description='Header Analysis')

        self.assertEqual(legacy_ac(user.id), 75)
        for service in ('reputation', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 75,
                             f"{service} did not see Header Analyzer's spend")

    def test_ac_remains_one_pool_not_four(self):
        user = make_user('ha_one_pool@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=100)

        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            deduct_service_credits(user.id, service, 25, ref_type='ip_check',
                                   description=service)

        self.assertEqual(legacy_ac(user.id), 0,
                         "25 on each of four services should exhaust one "
                         "100-credit pool, not four copies of it")
        for service in ('reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 0)

    def test_a_header_wallet_does_not_leak_into_the_other_three(self):
        user = make_user('ha_no_leak@example.com')
        add_service_credits(user.id, 'header_analysis', 40,
                            ref_type='service_purchase', ref_id='t')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=10)

        self.assertEqual(get_effective_balance(user.id, 'header_analysis'), 50)
        for service in ('reputation', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(get_effective_balance(user.id, service), 10,
                             f"{service} can see Header Analyzer's private wallet")

    def test_exhausted_ac_refuses_the_next_analysis_service(self):
        user = make_user('ha_exhausted@example.com')
        CurrentCredits.objects.create(user_id=user.id, ac_current_credits=2)

        deduct_service_credits(user.id, 'header_analysis', 1, ref_type='ip_check',
                               description='Header Analysis')
        deduct_service_credits(user.id, 'reputation', 1, ref_type='reputation',
                               description='Reputation Analysis')

        with self.assertRaises(InsufficientCredits):
            deduct_service_credits(user.id, 'ip_blocklist', 1, ref_type='ip_check',
                                   description='IP')
        self.assertEqual(legacy_ac(user.id), 0)
