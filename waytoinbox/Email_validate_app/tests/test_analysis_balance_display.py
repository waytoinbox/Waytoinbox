"""Phase 6, commit 10: what the four analysis surfaces SHOW.

Old-credit retirement update: each surface used to display the raw
CurrentCredits.ac_current_credits column, which was wrong in two directions:

  * a customer whose credits sat entirely in a new per-service wallet was shown
    0 while still being able to run the service;
  * the IP and Domain blocklist APIs reported get_vc_current_credit() — the
    Email Validation column — which was never an analysis balance at all.

Every surface now shows get_effective_balance(user, <its service>). With the
old credit system retired, that is that service's OWN ServiceCreditLot balance
only — the legacy AC pool behind it no longer contributes at all, and neither
does any other service's lot. Response key names and status codes are
unchanged.

The invariant these tests protect is the opposite of what it used to be. The
four analysis services used to share ONE legacy AC pool; now each is fully
private, so a correct display must never move because of another service's
lot or the retired legacy pool.
"""
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import (
    UserTable, CurrentCredits, ServiceCredit, CreditAuditLog,
)
from Email_validate_app.services.credit_manager import (
    get_effective_balance, deduct_service_credits,
)
from Email_validate_app.tests.credit_test_helpers import make_lot

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

    def test_each_page_shows_its_own_service_lot(self):
        for service, url in self.PAGES.items():
            make_lot(self.user.id, service, amount=7)

            shown, credits = self._shown(url)
            self.assertEqual(shown, 7, f'{url} did not show {service}')
            self.assertEqual(credits, 7, f'{url} "credits" disagrees')

    def test_each_page_no_longer_falls_back_to_the_shared_legacy_ac(self):
        """Old-credit retirement: legacy AC no longer counts toward the
        displayed balance -- a page funded only by it shows 0, exactly as if
        there were no credit at all."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=42)

        for url in self.PAGES.values():
            shown, _ = self._shown(url)
            self.assertEqual(shown, 0, f'{url} still shows the retired legacy pool')

    def test_a_pages_balance_is_only_its_own_lot_never_the_legacy_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        make_lot(self.user.id, 'ip_blocklist', amount=5)

        self.assertEqual(self._shown(self.PAGES['ip_blocklist'])[0], 5)

    def test_one_services_private_lot_is_not_shown_on_another_page(self):
        """The leak this commit had to avoid: reputation buys 40, and the other
        three pages must show 0 -- neither reputation's private lot nor the
        retired legacy pool."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        make_lot(self.user.id, 'reputation', amount=40)

        self.assertEqual(self._shown(self.PAGES['reputation'])[0], 40)
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(
                self._shown(self.PAGES[service])[0], 0,
                f'{service} page leaked reputation\'s private lot or legacy AC')

    def test_spending_one_services_lot_moves_only_that_page(self):
        """Old-credit retirement: there is no shared pool left to move every
        page at once -- each service's lot is private, so spending one
        leaves the other three exactly where they were."""
        for service in ANALYSIS:
            make_lot(self.user.id, service, amount=25)

        spent = set()
        for service in ANALYSIS:
            deduct_service_credits(self.user.id, service, 25,
                                   ref_type='ip_check', description=service)
            spent.add(service)
            for other_service, url in self.PAGES.items():
                shown, _ = self._shown(url)
                expected = 0 if other_service in spent else 25
                self.assertEqual(shown, expected,
                                 f'{url} disagrees after {service} spent')

    def test_the_legacy_plan_figures_are_left_alone(self):
        """ac_total_credits / ac_used_credits describe the legacy subscription
        grant, not the new lot, and were deliberately not changed. The legacy
        ac_current_credits column itself no longer contributes to the
        displayed balance at all."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10,
                                      ac_total_credits=60, ac_used_credits=50)
        make_lot(self.user.id, 'reputation', amount=5)

        r = self.client.get(self.PAGES['reputation'])
        self.assertEqual(r.context['ac_current_credits'], 5)
        self.assertEqual(r.context['ac_total_credits'], 60)
        self.assertEqual(r.context['ac_used_credits'], 50)

    def test_rendering_a_page_never_deducts(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=100)
        for service in ANALYSIS:
            make_lot(self.user.id, service, amount=5)
        before = self.audit_rows()

        for _ in range(3):
            for url in self.PAGES.values():
                self.client.get(url)

        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).ac_current_credits, 100)
        for service in ANALYSIS:
            self.assertEqual(get_effective_balance(self.user.id, service), 5)
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
        make_lot(self.user.id, 'ip_blocklist', amount=5)

        body = self._add_ip().json()

        self.assertEqual(body['status'], 'ok')
        self.assertIn('ip_current_credits', body)          # key unchanged
        self.assertEqual(body['ip_current_credits'], 4)    # 5 - 1, not 9999
        self.assertNotEqual(body['ip_current_credits'], 9999)

    def test_domain_api_no_longer_reports_the_validation_column(self):
        CurrentCredits.objects.create(user_id=self.user.id,
                                      vc_current_credits=9999, ac_current_credits=0)
        make_lot(self.user.id, 'domain_blocklist', amount=5)

        body = self._add_domain().json()

        self.assertEqual(body['status'], 'ok')
        # The domain response's key really is "ip_current_credits" — a
        # pre-existing quirk, deliberately preserved.
        self.assertIn('ip_current_credits', body)
        self.assertEqual(body['ip_current_credits'], 4)
        self.assertNotEqual(body['ip_current_credits'], 9999)

    def test_ip_api_no_longer_falls_back_to_the_legacy_pool(self):
        """Old-credit retirement: legacy AC alone can no longer fund the add
        -- it fails exactly as if there were no credit at all, and the
        legacy pool is left untouched."""
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=30)

        body = self._add_ip().json()

        self.assertEqual(body['status'], 'error')
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).ac_current_credits, 30)

    def test_domain_api_no_longer_falls_back_to_the_legacy_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=30)

        body = self._add_domain().json()

        self.assertEqual(body['status'], 'error')
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).ac_current_credits, 30)

    def test_the_ip_api_cannot_spend_another_services_lot_or_the_legacy_pool(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=10)
        make_lot(self.user.id, 'reputation', amount=40)

        body = self._add_ip().json()

        # Neither reputation's private lot nor the legacy pool can fund this.
        self.assertEqual(body['status'], 'error')
        self.assertEqual(
            CurrentCredits.objects.get(user_id=self.user.id).ac_current_credits, 10)

    def test_existing_status_codes_and_keys_are_unchanged(self):
        make_lot(self.user.id, 'ip_blocklist', amount=5)
        make_lot(self.user.id, 'domain_blocklist', amount=5)

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
        make_lot(self.user.id, 'ip_blocklist', amount=5)
        before = self.audit_rows()

        self.client.post('/api/blocklist/ip/', {'ip': 'not-an-ip'})

        self.assertEqual(self.audit_rows(), before)
        self.assertEqual(get_effective_balance(self.user.id, 'ip_blocklist'), 5)


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

    def test_it_reports_the_header_lot_which_the_ip_add_does_not_touch(self):
        """Old-credit retirement: the two lots are private now, so adding an
        IP monitor -- which spends from ip_blocklist's own lot -- does not
        move header_analysis's balance at all; the reported figure stays
        put rather than dropping by 1."""
        make_lot(self.user.id, 'header_analysis', amount=3)
        make_lot(self.user.id, 'ip_blocklist', amount=5)

        page = self.client.get('/Header_Analysis/')
        before = page.context['ac_current_credits']
        self.assertEqual(before, 3)

        body = self._post(ip='203.0.113.90').json()

        self.assertEqual(body['ac_current_credits'], 3)
        self.assertEqual(
            self.client.get('/Header_Analysis/').context['ac_current_credits'], 3)

    def test_the_response_key_is_unchanged(self):
        make_lot(self.user.id, 'header_analysis', amount=5)
        make_lot(self.user.id, 'ip_blocklist', amount=5)

        body = self._post(ip='203.0.113.91').json()

        self.assertEqual(sorted(body.keys()),
                         sorted(['status', 'domain', 'ip', 'credits_used',
                                 'ac_current_credits']))
        self.assertEqual(body['status'], 'ok')


# ── The invariant, read through the surfaces ──────────────────────────────────

class DisplayInvariantTests(_Base):

    def test_display_never_conflates_the_four_services_lots(self):
        """Old-credit retirement: there is no shared pool left for the four
        pages to display in common -- each shows only its own private lot,
        so funding all four identically and then draining just one moves
        only that page."""
        for service in ANALYSIS:
            make_lot(self.user.id, service, amount=100)

        pages = {
            'reputation':       '/Reputation_Analysis/',
            'header_analysis':  '/Header_Analysis/',
            'ip_blocklist':     '/Blocklist_Monitor/',
            'domain_blocklist': '/Domain_Blacklist/',
        }
        shown = {s: self.client.get(u).context['ac_current_credits']
                 for s, u in pages.items()}
        self.assertEqual(set(shown.values()), {100})

        # Draining reputation's own lot leaves the other three untouched.
        deduct_service_credits(self.user.id, 'reputation', 100,
                               ref_type='ip_check', description='drain')
        shown = {s: self.client.get(u).context['ac_current_credits']
                 for s, u in pages.items()}
        self.assertEqual(shown['reputation'], 0)
        for service in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(shown[service], 100)

    def test_no_service_credit_row_is_created_by_displaying(self):
        CurrentCredits.objects.create(user_id=self.user.id, ac_current_credits=50)

        for url in ('/Reputation_Analysis/', '/Header_Analysis/',
                    '/Blocklist_Monitor/', '/Domain_Blacklist/'):
            self.client.get(url)

        self.assertEqual(
            ServiceCredit.objects.filter(user_id=self.user.id).count(), 0)
