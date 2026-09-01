"""Phase 6, commit 10: what the four analysis surfaces SHOW.

No billing logic changed here. Each surface used to display the raw
CurrentCredits.ac_current_credits column, which was wrong in two directions:

  * a customer whose credits sat entirely in a new per-service wallet was shown
    0 while still being able to run the service;
  * the IP and Domain blocklist APIs reported get_vc_current_credit() — the
    Email Validation column — which was never an analysis balance at all.

Every surface now shows get_effective_balance(user, <its service>): that
service's own wallet plus the shared legacy AC pool behind it. Response key
names and status codes are unchanged.

The invariant these tests protect is subtle. The four services share ONE legacy
AC pool but each has a PRIVATE wallet, so a correct display has to move with
the shared pool while never revealing another service's private balance.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog,
)
from Email_validate_app.services.credit_manager import (
    add_service_credits, deduct_service_credits,
)

ANALYSIS = ('reputation', 'header_analysis', 'ip_blocklist', 'domain_blocklist')


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Display', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class _Base(TestCase):
    def setUp(self):
        self.user = make_user(f'{self.__class__.__name__.lower()}@example.com')
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def audit_rows(self):
        return CreditAuditLog.objects.filter(user_id=self.user.id).count()


# ── The four page surfaces ────────────────────────────────────────────────────

class PageBalanceTests(_Base):
    """Each page renders its own service's effective balance."""

    PAGES = {
        'reputation':       '/Reputation_Analysis/',
        'header_analysis':  '/Header_Analysis/',
        'ip_blocklist':     '/Blocklist_Monitor/',
        'domain_blocklist': '/Domain_Blacklist/',
    }

    def _shown(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.context['ac_current_credits'], r.context['credits']

    def test_each_page_shows_its_own_service_wallet(self):
        for service, url in self.PAGES.items():
            ServiceCredit.objects.filter(user_id=self.user.id).delete()
            add_service_credits(self.user.id, service, 7,
                                ref_type='service_purchase', ref_id='t')

            shown, credits = self._shown(url)
            self.assertEqual(shown, 7, f'{url} did not show {service}')
            self.assertEqual(credits, 7, f'{url} "credits" disagrees')

    def test_each_page_falls_back_to_the_shared_legacy_ac(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=42)

        for url in self.PAGES.values():
            shown, _ = self._shown(url)
            self.assertEqual(shown, 42, f'{url} did not show the legacy pool')

    def test_a_page_adds_its_private_wallet_to_the_shared_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self._shown(self.PAGES['ip_blocklist'])[0], 15)

    def test_one_services_private_wallet_is_not_shown_on_another_page(self):
        """The leak this commit had to avoid: reputation buys 40, and the other
        three pages must still show only the shared 10."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        add_service_credits(self.user.id, 'reputation', 40,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self._shown(self.PAGES['reputation'])[0], 50)
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(
                self._shown(self.PAGES[service])[0], 10,
                f'{service} page leaked reputation\'s private wallet')

    def test_spending_the_shared_pool_moves_every_page(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=100)

        expected = [75, 50, 25, 0]
        for service, remaining in zip(ANALYSIS, expected):
            deduct_service_credits(self.user.id, service, 25,
                                   ref_type='ip_check', description=service)
            for url in self.PAGES.values():
                self.assertEqual(self._shown(url)[0], remaining,
                                 f'{url} disagrees after {service} spent')

    def test_the_legacy_plan_figures_are_left_alone(self):
        """ac_total_credits / ac_used_credits describe the legacy subscription
        grant, not the new wallet, and were deliberately not changed."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10,
                                      ac_total_credits=60, ac_used_credits=50)
        add_service_credits(self.user.id, 'reputation', 5,
                            ref_type='service_purchase', ref_id='t')

        r = self.client.get(self.PAGES['reputation'])
        self.assertEqual(r.context['ac_current_credits'], 15)
        self.assertEqual(r.context['ac_total_credits'], 60)
        self.assertEqual(r.context['ac_used_credits'], 50)

    def test_rendering_a_page_never_deducts(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=100)
        for service in ANALYSIS:
            add_service_credits(self.user.id, service, 5,
                                ref_type='service_purchase', ref_id='t')
        before = self.audit_rows()

        for _ in range(3):
            for url in self.PAGES.values():
                self.client.get(url)

        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).ac_current_credits, 100)
        for service in ANALYSIS:
            self.assertEqual(
                ServiceCredit.objects.get(user_id=self.user.id, service=service).balance, 5)
        self.assertEqual(self.audit_rows(), before)


# ── The API surfaces ──────────────────────────────────────────────────────────

class ApiBalanceTests(_Base):
    """The IP and Domain endpoints reported the Email Validation column."""

    def _add_ip(self, ip='203.0.113.77'):
        with patch('Email_validate_app.views.api.ip_blacklists',
                   return_value={'spamhaus': 'Not Listed'}):
            return self.client.post('/api/blocklist/ip/', {'ip': ip})

    def _add_domain(self, domain='display-check.com'):
        with patch('Email_validate_app.views.api.domain_blacklists',
                   return_value={'spamhaus': 'Not Listed'}):
            return self.client.post('/api/blocklist/domain/', {'domain': domain})

    def test_ip_api_no_longer_reports_the_validation_column(self):
        # A large VC balance must not show up as an analysis balance.
        CurrentCredits.objects.create(user_id=self.user.id,
                                      vc_current_credits=9999, ac_current_credits=0)
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._add_ip().json()

        self.assertEqual(body['status'], 'ok')
        self.assertIn('ip_current_credits', body)          # key unchanged
        self.assertEqual(body['ip_current_credits'], 4)    # 5 - 1, not 9999
        self.assertNotEqual(body['ip_current_credits'], 9999)

    def test_domain_api_no_longer_reports_the_validation_column(self):
        CurrentCredits.objects.create(user_id=self.user.id,
                                      vc_current_credits=9999, ac_current_credits=0)
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._add_domain().json()

        self.assertEqual(body['status'], 'ok')
        # The domain response's key really is "ip_current_credits" — a
        # pre-existing quirk, deliberately preserved.
        self.assertIn('ip_current_credits', body)
        self.assertEqual(body['ip_current_credits'], 4)
        self.assertNotEqual(body['ip_current_credits'], 9999)

    def test_ip_api_reports_the_legacy_pool_when_the_wallet_is_empty(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=30)

        body = self._add_ip().json()

        self.assertEqual(body['ip_current_credits'], 29)

    def test_domain_api_reports_the_legacy_pool_when_the_wallet_is_empty(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=30)

        body = self._add_domain().json()

        self.assertEqual(body['ip_current_credits'], 29)

    def test_the_ip_api_does_not_report_another_services_wallet(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        add_service_credits(self.user.id, 'reputation', 40,
                            ref_type='service_purchase', ref_id='t')

        body = self._add_ip().json()

        # 10 shared, minus the 1 just spent. Reputation's 40 is invisible here.
        self.assertEqual(body['ip_current_credits'], 9)

    def test_existing_status_codes_and_keys_are_unchanged(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        add_service_credits(self.user.id, 'domain_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        self.assertEqual(self.client.get('/api/blocklist/ip/').status_code, 405)
        self.assertEqual(self.client.post('/api/blocklist/ip/', {'ip': ''}).status_code, 400)
        self.assertEqual(self.client.post('/api/blocklist/ip/', {'ip': 'x'}).status_code, 400)
        self.assertEqual(self.client.get('/api/blocklist/domain/').status_code, 405)
        self.assertEqual(self.client.post('/api/blocklist/domain/', {'domain': ''}).status_code, 400)

        ok = self._add_ip().json()
        self.assertEqual(sorted(ok.keys()),
                         sorted(['status', 'ip', 'ip_id', 'listed_count',
                                 'ip_current_credits']))

    def test_a_rejected_request_still_charges_nothing(self):
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')
        before = self.audit_rows()

        self.client.post('/api/blocklist/ip/', {'ip': 'not-an-ip'})

        self.assertEqual(self.audit_rows(), before)
        self.assertEqual(
            ServiceCredit.objects.get(user_id=self.user.id,
                                      service='ip_blocklist').balance, 5)


class AddToMonitorsBalanceTests(_Base):
    """add_to_monitors is called only from the Header Analyzer page, whose
    credit bar it refreshes — so it reports that page's metric."""

    URL = '/api/add-to-monitors/'

    def _post(self, **data):
        with patch('Email_validate_app.views.blocklist.ip_blacklists',
                   return_value={'spamhaus': 'Not Listed'}), \
             patch('Email_validate_app.views.blocklist.domain_blacklists',
                   return_value={'spamhaus': 'Not Listed'}):
            return self.client.post(self.URL, data)

    def test_it_reports_the_same_metric_the_header_page_renders(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=20)
        add_service_credits(self.user.id, 'header_analysis', 3,
                            ref_type='service_purchase', ref_id='t')

        page = self.client.get('/Header_Analysis/')
        before = page.context['ac_current_credits']
        self.assertEqual(before, 23)

        body = self._post(ip='203.0.113.90').json()

        # The IP add spent 1 from the shared pool, so the header bar drops by 1
        # rather than switching to some unrelated number.
        self.assertEqual(body['ac_current_credits'], 22)
        self.assertEqual(
            self.client.get('/Header_Analysis/').context['ac_current_credits'], 22)

    def test_the_response_key_is_unchanged(self):
        add_service_credits(self.user.id, 'header_analysis', 5,
                            ref_type='service_purchase', ref_id='t')
        add_service_credits(self.user.id, 'ip_blocklist', 5,
                            ref_type='service_purchase', ref_id='t')

        body = self._post(ip='203.0.113.91').json()

        self.assertEqual(sorted(body.keys()),
                         sorted(['status', 'domain', 'ip', 'credits_used',
                                 'ac_current_credits']))
        self.assertEqual(body['status'], 'ok')


# ── The invariant, read through the surfaces ──────────────────────────────────

class DisplayInvariantTests(_Base):

    def test_display_never_multiplies_the_shared_pool(self):
        """Summing what the four pages show is NOT the user's total. Each shows
        the same shared pool plus its own wallet; this asserts the shared half
        is one balance, not four."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=100)

        pages = {
            'reputation':       '/Reputation_Analysis/',
            'header_analysis':  '/Header_Analysis/',
            'ip_blocklist':     '/Blocklist_Monitor/',
            'domain_blocklist': '/Domain_Blacklist/',
        }
        shown = {s: self.client.get(u).context['ac_current_credits']
                 for s, u in pages.items()}
        self.assertEqual(set(shown.values()), {100})

        # Spending 100 through any one of them empties every display.
        deduct_service_credits(self.user.id, 'reputation', 100,
                               ref_type='ip_check', description='drain')
        shown = {s: self.client.get(u).context['ac_current_credits']
                 for s, u in pages.items()}
        self.assertEqual(set(shown.values()), {0})

    def test_no_service_credit_row_is_created_by_displaying(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        for url in ('/Reputation_Analysis/', '/Header_Analysis/',
                    '/Blocklist_Monitor/', '/Domain_Blacklist/'):
            self.client.get(url)

        self.assertEqual(
            ServiceCredit.objects.filter(user_id=self.user.id).count(), 0)
