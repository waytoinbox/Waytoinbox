"""Phase 6, commit 12: service credit balance UI redesign.

Display only. No deduction logic, no checkout/coupon/pricing code, and no API
response shape changed in this commit — only templates, the profile view's
read-only context (_service_balance_rows), and the nav_credits context
processor.

DATABASE SAFETY — read this before touching this file
=======================================================
This project's `manage.py test` cannot create an isolated test database: a
pre-existing migration applies a duplicate `user_id` column and aborts
mid-migrate (confirmed again while preparing this commit — the attempt fails
cleanly with `OperationalError: (1060, "Duplicate column name 'user_id'")
without ever touching the real database, because Django only ever writes to
the separately-named `test_<dbname>` schema, never the configured `default`
one).

A prior turn in this project's history ran a Django `TransactionTestCase`
(`test_coupons.py::CouponConcurrencyTests`) through `manage.py shell` instead
of `manage.py test`. `shell` never creates or switches to an isolated test
database, so that TransactionTestCase's teardown — which flushes every
Django-managed table — ran directly against the live local database and
wiped every user record, credit balance, and audit-log row it held. The large
ad-hoc `ivc_*` bulk-validation-result tables survived only because they sit
outside Django's migrations and therefore outside `flush`'s reach.

Because of that, EVERY test in this file is a `SimpleTestCase` with its
`databases` attribute left at the default empty set, which makes Django raise
immediately if any test here so much as opens a database connection. Every
credit-manager / ORM call is mocked. Nothing here can touch the real
database, the (currently non-creatable) test database, or any other schema —
by construction, not by care taken while writing it.

The two full-page render tests load the ACTUAL template files from disk via
Django's real template engine — not copies — but supply every needed value by
hand and never pass `request=` to `.render()`, which is what would otherwise
trigger Django's registered context processors (including nav_credits, which
does query the database) to run automatically.

Once the migration inconsistency is fixed and `manage.py test` can build a
real isolated database, the integration-level suite this file's docstring in
earlier revisions described (Client()-driven, hitting the real views) should
be restored as a companion to these logic/rendering tests, not a replacement
for them.
"""
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

ANALYSIS_SERVICES = ('reputation', 'header_analysis', 'ip_blocklist', 'domain_blocklist')
ALL_SEVEN = ('email_validation', 'email_marketing', 'sales_outreach',
             'reputation', 'header_analysis', 'ip_blocklist', 'domain_blocklist')


def _canned_balances(overrides=None, ac=0):
    """A get_all_service_balances()-shaped dict with every service at 0
    except what `overrides` sets, and the shared AC pool at `ac`. No trial
    active by default -- 'trial': 0 per service, trial_active False -- these
    tests are about the wallet/legacy figures, not the trial system (see
    test_trial_system.py for that)."""
    services = {key: {'new': 0, 'legacy': 0, 'trial': 0, 'effective': 0} for key in ALL_SEVEN}
    if overrides:
        for key, effective in overrides.items():
            services[key]['effective'] = effective
    return {
        'services': services,
        'legacy_shared': {'ac': ac, 'vc': 0, 'cc': 0},
        'trial_active': False,
        'trial_ends_at': None,
    }


class FakeRequest:
    """Just enough of an HttpRequest for `{% if 'logged_in' in request.session %}`
    and friends — no session backend, no middleware, no database."""
    def __init__(self, logged_in='fixture@example.com'):
        self.session = {'logged_in': logged_in} if logged_in else {}
        self.path = '/'
        self.META = {}
        self.GET = {}


def render_real_template(name, context):
    """Render an actual template file from disk with the given context dict.

    Deliberately calls `.render(context)` with no `request=` argument: passing
    one would make Django invoke every registered context processor —
    including nav_credits, which queries the database — automatically. Every
    context value the page needs must therefore be supplied explicitly here.
    """
    from django.template.loader import get_template
    return get_template(name).render(context)


# ── _service_balance_rows(): the profile page's per-service aggregation ──────

class ServiceBalanceRowsLogicTests(SimpleTestCase):
    """views.profile._service_balance_rows(), with get_all_service_balances()
    mocked so no query is ever made."""

    def _call(self, canned):
        from Email_validate_app.views.profile import _service_balance_rows
        with patch('Email_validate_app.services.credit_manager.get_all_service_balances',
                   return_value=canned) as mocked:
            rows, shared_ac, trial_active, trial_ends_at = _service_balance_rows(user_id=1)
            return rows, shared_ac, trial_active, trial_ends_at, mocked

    def test_all_seven_services_are_returned_in_order(self):
        rows, _, _, _, _ = self._call(_canned_balances())
        self.assertEqual(tuple(r['key'] for r in rows), ALL_SEVEN)

    def test_labels_are_the_canonical_service_names(self):
        from Email_validate_app.services.pricing import SERVICE_LABELS
        rows, _, _, _, _ = self._call(_canned_balances())
        for row in rows:
            self.assertEqual(row['label'], SERVICE_LABELS[row['key']])

    def test_zero_balance_is_zero_not_none_or_blank(self):
        rows, shared_ac, _, _, _ = self._call(_canned_balances())
        for row in rows:
            self.assertEqual(row['balance'], 0)
        self.assertEqual(shared_ac, 0)

    def test_service_wallet_balance_passes_through(self):
        rows, _, _, _, _ = self._call(_canned_balances({'sales_outreach': 3}))
        by_key = {r['key']: r for r in rows}
        self.assertEqual(by_key['sales_outreach']['balance'], 3)

    def test_legacy_fallback_is_reflected_in_the_effective_figure(self):
        # get_all_service_balances() already folds the legacy pool into
        # 'effective' — this asserts the helper passes that through untouched
        # rather than re-deriving or double-counting it.
        rows, _, _, _, _ = self._call(_canned_balances({'email_validation': 42}))
        by_key = {r['key']: r for r in rows}
        self.assertEqual(by_key['email_validation']['balance'], 42)

    def test_only_the_four_analysis_services_are_flagged_shared(self):
        rows, _, _, _, _ = self._call(_canned_balances())
        by_key = {r['key']: r for r in rows}
        for key in ANALYSIS_SERVICES:
            self.assertTrue(by_key[key]['shared'], f'{key} should be flagged shared')
        for key in ('email_validation', 'email_marketing', 'sales_outreach'):
            self.assertFalse(by_key[key]['shared'], f'{key} must not be flagged shared')

    def test_shared_ac_pool_is_reported_once_not_per_service(self):
        rows, shared_ac, _, _, _ = self._call(_canned_balances(ac=77))
        self.assertEqual(shared_ac, 77)
        # It is a single return value, not one per row — there is no key on
        # any row that could be mistaken for "this service's own copy of AC".
        for row in rows:
            self.assertNotIn('shared_ac', row)

    def test_one_services_private_wallet_does_not_change_another_rows_number(self):
        rows, _, _, _, _ = self._call(_canned_balances({'reputation': 50}, ac=10))
        by_key = {r['key']: r for r in rows}
        self.assertEqual(by_key['reputation']['balance'], 50)
        for key in ('header_analysis', 'ip_blocklist', 'domain_blocklist'):
            self.assertEqual(by_key[key]['balance'], 0,
                             f"{key} balance should come from its own dict entry, "
                             f"not reputation's")

    def test_the_aggregate_is_fetched_exactly_once(self):
        _, _, _, _, mocked = self._call(_canned_balances())
        mocked.assert_called_once_with(1)

    def test_trial_remaining_passes_through_per_service(self):
        canned = _canned_balances()
        canned['services']['sales_outreach']['trial'] = 1
        canned['trial_active'] = True
        rows, _, trial_active, _, _ = self._call(canned)
        by_key = {r['key']: r for r in rows}
        self.assertEqual(by_key['sales_outreach']['trial_remaining'], 1)
        self.assertEqual(by_key['email_validation']['trial_remaining'], 0)
        self.assertTrue(trial_active)


# ── nav_credits(): the global nav badge context processor ────────────────────

class NavCreditsContextProcessorLogicTests(SimpleTestCase):

    def _call(self, canned, logged_in='fixture@example.com'):
        from Email_validate_app import context_processors as cp
        request = FakeRequest(logged_in)
        # trial_started_at/trial_ends_at must be real None, not an
        # auto-generated MagicMock attribute -- nav_credits() compares
        # trial_ends_at > now(), and a bare MagicMock has no meaningful
        # ordering against a datetime (it would raise, which nav_credits'
        # own try/except would then swallow into an unexpected {}).
        fake_user = MagicMock(id=1, trial_started_at=None, trial_ends_at=None)
        with patch('Email_validate_app.context_processors.get_all_service_balances',
                   return_value=canned) as balances_mock, \
             patch('Email_validate_app.models.UserTable.objects') as manager_mock:
            manager_mock.filter.return_value.first.return_value = fake_user
            result = cp.nav_credits(request)
            return result, balances_mock, manager_mock

    def test_logged_out_request_gets_an_empty_context_and_makes_no_query(self):
        result, balances_mock, manager_mock = self._call(_canned_balances(), logged_in=None)
        self.assertEqual(result, {})
        balances_mock.assert_not_called()
        manager_mock.filter.assert_not_called()

    def test_logged_in_request_reports_effective_validation_and_marketing(self):
        canned = _canned_balances({'email_validation': 100, 'email_marketing': 60}, ac=25)
        result, _, _ = self._call(canned)
        self.assertEqual(result['nav_validation_credits'], 100)
        self.assertEqual(result['nav_marketing_credits'], 60)
        self.assertEqual(result['nav_shared_analysis_credits'], 25)

    def test_validation_badge_reflects_the_new_wallet_not_bare_legacy_vc(self):
        """The bug this commit fixes: the nav badge never migrated off the raw
        VC column when Email Validation got its own wallet in commit 1."""
        canned = _canned_balances({'email_validation': 15})
        result, _, _ = self._call(canned)
        self.assertEqual(result['nav_validation_credits'], 15)

    def test_an_exception_yields_an_empty_context_not_a_crash(self):
        from Email_validate_app import context_processors as cp
        request = FakeRequest()
        with patch('Email_validate_app.context_processors.get_all_service_balances',
                   side_effect=RuntimeError('boom')), \
             patch('Email_validate_app.models.UserTable.objects') as manager_mock:
            manager_mock.filter.return_value.first.return_value = MagicMock(id=1)
            self.assertEqual(cp.nav_credits(request), {})


# ── Full-page rendering: the real template files, zero database access ───────

class AnalysisPageRenderingTests(SimpleTestCase):
    """Loads the actual template files from disk. No request= is passed to
    render(), so no context processor — including the database-touching
    nav_credits — ever runs."""

    PAGES = {
        'i_Reputation_Analysis.html': ('raCreditText', 86),
        'i_header_analysis.html':     ('hacText', 86),
        'i_ip_blocklist.html':        ('ipcText', 86),
        'i_domain_blocklist.html':    ('blCreditText', 86),
    }

    def _context(self, current=86, total=0):
        return {
            'request': FakeRequest(),
            'ac_current_credits': current,
            'ac_total_credits': total,
            'plan_name': None,
            'plan_valid_till': None,
            'pf_statuses': [],
        }

    def test_each_analysis_page_shows_available_credits_not_a_ratio(self):
        import re
        for template, (elem_id, value) in self.PAGES.items():
            html = render_real_template(template, self._context(current=value, total=99999))
            self.assertIn('Available Credits', html, template)
            # The visible credit-text span holds only the current balance —
            # the legacy total (99999) is still passed into the page context
            # (kept for the now-unused JS variable, harmless) but must never
            # appear next to it as a "current / total" ratio.
            self.assertEqual(re.findall(rf'{elem_id}">(.*?)</span>', html),
                             [str(value)], template)
            self.assertNotIn(f'{value} / 99999', html, template)

    def test_zero_balance_renders_as_zero(self):
        for template, (elem_id, _) in self.PAGES.items():
            html = render_real_template(template, self._context(current=0))
            self.assertEqual(__import__('re').findall(rf'{elem_id}">(.*?)</span>', html),
                             ['0'], template)

    def test_shared_hint_present_on_every_analysis_page(self):
        for template in self.PAGES:
            html = render_real_template(template, self._context())
            self.assertIn('Uses shared Analysis Credits when available', html, template)

    def test_no_bare_vc_ac_cc_labels_leak_onto_analysis_pages(self):
        import re
        for template in self.PAGES:
            html = render_real_template(template, self._context())
            self.assertFalse(re.search(r'>\s*(VC|AC|CC)\s*<', html), template)

    def test_rendering_a_page_makes_no_database_call(self):
        # SimpleTestCase already forbids DB access for the whole class; this
        # just makes the intent explicit at the point of use.
        for template in self.PAGES:
            render_real_template(template, self._context())


class ProfilePageRenderingTests(SimpleTestCase):

    def _context(self, service_balances=None, shared_ac=0, validation=0, marketing=0):
        from Email_validate_app.services.pricing import SERVICE_LABELS
        rows = service_balances or [
            {'key': key, 'label': SERVICE_LABELS[key], 'balance': 0,
             'shared': key in ANALYSIS_SERVICES}
            for key in ALL_SEVEN
        ]
        return {
            'request': FakeRequest(),
            'username': 'Fixture User',
            'user_email': 'fixture@example.com',
            'service_balances': rows,
            'shared_ac_balance': shared_ac,
            'hero_validation_credits': validation,
            'hero_marketing_credits': marketing,
        }

    def test_all_seven_services_are_listed_on_the_billing_tab(self):
        html = render_real_template('i_profile.html', self._context())
        self.assertEqual(html.count('credits available'), 7)

    def test_shared_hint_appears_once_per_analysis_service_row(self):
        html = render_real_template('i_profile.html', self._context())
        self.assertEqual(
            html.count('Uses shared Analysis Credits when available'),
            len(ANALYSIS_SERVICES))

    def test_no_x_of_y_pattern_anywhere_on_the_page(self):
        import re
        rows = [
            {'key': 'email_validation', 'label': 'Email Validation', 'balance': 500, 'shared': False},
            {'key': 'reputation', 'label': 'Reputation Analysis', 'balance': 50, 'shared': True},
        ]
        html = render_real_template('i_profile.html',
                                    self._context(service_balances=rows,
                                                  shared_ac=50, validation=500))
        self.assertEqual(re.findall(r'>\s*\d[\d,]*\s*/\s*\d[\d,]*\s*<', html), [])

    def test_no_raw_vc_ac_cc_labels_remain(self):
        html = render_real_template('i_profile.html', self._context())
        self.assertNotIn('Validation Credits', html)
        self.assertNotIn('Contact Credits', html)

    def test_hero_strip_shows_the_effective_validation_and_marketing_figures(self):
        html = render_real_template(
            'i_profile.html', self._context(validation=500, marketing=300, shared_ac=10))
        self.assertIn('500', html)
        self.assertIn('300', html)

    def test_private_wallet_does_not_leak_into_another_services_row(self):
        rows = [
            {'key': 'reputation', 'label': 'Reputation Analysis', 'balance': 50, 'shared': True},
            {'key': 'header_analysis', 'label': 'Email Header Analyzer', 'balance': 10, 'shared': True},
            {'key': 'ip_blocklist', 'label': 'IP Blocklist Monitor', 'balance': 10, 'shared': True},
            {'key': 'domain_blocklist', 'label': 'Domain Blocklist Monitor', 'balance': 10, 'shared': True},
        ]
        html = render_real_template('i_profile.html',
                                    self._context(service_balances=rows, shared_ac=10))
        # Each distinct balance value should appear against exactly one row's
        # worth of "credits available" text — 50 exactly once, 10 three times.
        import re
        numbers = re.findall(r'>\s*([\d,]+)\s*<span[^>]*>credits available', html)
        self.assertEqual(numbers.count('50'), 1)
        self.assertEqual(numbers.count('10'), 3)

    def test_billing_tab_deep_link_markup_is_present(self):
        html = render_real_template('i_profile.html', self._context())
        self.assertIn('data-tab="billing"', html)
        self.assertIn("new URLSearchParams(window.location.search).get('tab')", html)

    def test_rendering_the_page_makes_no_database_call(self):
        render_real_template('i_profile.html', self._context())


class NavBadgeRenderingTests(SimpleTestCase):
    """i_index.html's topbar_credit block — the real file, real Django
    rendering, the nav_credits values supplied directly rather than through
    the (database-touching) context processor."""

    def _context(self, validation=0, shared_ac=0, marketing=0, logged_in=True):
        return {
            'request': FakeRequest('fixture@example.com' if logged_in else None),
            'nav_validation_credits': validation,
            'nav_shared_analysis_credits': shared_ac,
            'nav_marketing_credits': marketing,
        }

    def test_nav_badge_shows_the_three_supplied_values(self):
        html = render_real_template('i_index.html',
                                    self._context(validation=1234, shared_ac=77, marketing=55))
        self.assertIn('>1234<', html)
        self.assertIn('>77<', html)
        self.assertIn('>55<', html)

    def test_shared_analysis_appears_exactly_once_not_per_service(self):
        html = render_real_template('i_index.html', self._context(shared_ac=50))
        self.assertEqual(html.count('credit-badge--ac'), 1)

    def test_no_bare_vc_ac_cc_letter_labels_remain(self):
        import re
        html = render_real_template('i_index.html', self._context())
        self.assertFalse(re.search(r'class="credit-badge-label">(VC|AC|CC)<', html))

    def test_nav_badge_links_to_the_billing_tab(self):
        html = render_real_template('i_index.html', self._context())
        self.assertIn('/profile/?tab=billing', html)

    def test_logged_out_request_renders_no_credit_badge(self):
        html = render_real_template('i_index.html', self._context(logged_in=False))
        self.assertNotIn('credit-badge-group', html)

    def test_rendering_makes_no_database_call(self):
        render_real_template('i_index.html', self._context())
