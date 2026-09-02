"""Tests for Phase 7 follow-ups:

  1. Embedding the new service-credit purchase cards into the old
     i_pricing.html page's Subscription tab, in place of the
     Classic/Standard/Advanced plan cards, while leaving everything else on
     that page unchanged.
  2. Restyling those cards (on both i_pricing.html and the standalone
     i_subscription.html) into a two-column layout that reuses PAYG's own
     .payg-wrap/.payg-card classes, so spacing/padding/gap/card styling
     match exactly rather than being hand-duplicated.
  3. Giving i_subscription.html the same page chrome as i_pricing.html
     (notice bar, hero, footer) minus the sidebar, via an opt-in
     body_class hook on i_index.html that leaves every other page unchanged.

Covers:
  * /pricing/ still renders for both anonymous and logged-in users.
  * The Subscription tab now contains the service-credit cards (steppers,
    quote/order/verify URLs, all seven service rows) instead of the old
    Classic/Standard/Advanced plan cards, which are gone.
  * The Subscription tab appears before Pay-As-You-Go in the rendered HTML.
  * The Pay-As-You-Go panel itself is untouched.
  * Both pages' subscription cards use the two-column .payg-wrap/.payg-card
    layout, not the old single-column .sc-page/.sc-card.
  * i_subscription.html has the notice bar, hero heading and footer, and
    body class "no-sidebar"; i_pricing.html keeps its sidebar.
  * The sidebar-hiding mechanism is additive: a page with no body_class
    override still gets an ordinary (sidebar-shown) body tag.
"""
from django.test import TestCase, Client, override_settings

from Email_validate_app.models import UserTable


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Pricing Page Test', user_email=email, password='StrongPass123!')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class PricingPageTests(TestCase):

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _login(self):
        user = make_user('pricing_page@example.com')
        session = self.client.session
        session['logged_in'] = user.user_email
        session.save()
        return user

    def test_pricing_page_renders_for_anonymous_user(self):
        r = self.client.get('/pricing/')
        self.assertEqual(r.status_code, 200)

    def test_pricing_page_renders_for_logged_in_user(self):
        self._login()
        r = self.client.get('/pricing/')
        self.assertEqual(r.status_code, 200)

    def test_subscription_tab_contains_the_new_service_credit_cards(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        self.assertIn('id="panel-subscription"', html)
        self.assertIn('id="scTotal"', html)
        self.assertIn('id="scGetStarted"', html)
        self.assertIn('id="scModal"', html)
        for service in ('email_validation', 'email_marketing', 'sales_outreach',
                        'reputation', 'header_analysis', 'ip_blocklist',
                        'domain_blocklist'):
            self.assertIn(f'data-service="{service}"', html)
        self.assertIn('subscription/quote/', html)
        self.assertIn('subscription/order/', html)
        self.assertIn('subscription/verify/', html)

    def test_old_subscription_plan_cards_are_gone(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        for removed in ('ccsSlider', 'ccsPlansGrid', 'ccsClassicPrice',
                        'ccsStandardPrice', 'ccsAdvancedPrice', 'Get Classic',
                        'Get Standard', 'Get Advanced', 'ccs-billing-toggle'):
            self.assertNotIn(removed, html, removed)

    def test_subscription_tab_button_comes_before_payg_tab_button(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        sub_pos  = html.index("switchPricingTab('subscription'")
        payg_pos = html.index("switchPricingTab('payg'")
        self.assertLess(sub_pos, payg_pos,
                        "Subscription tab button must come before Pay-As-You-Go")

    def test_subscription_panel_is_the_default_active_one(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        self.assertIn('class="pricing-panel active" id="panel-subscription"', html)
        self.assertIn('class="pricing-panel" id="panel-payg"', html)

    def test_payg_panel_is_unchanged(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        self.assertIn('How many emails do you have?', html)
        self.assertIn('id="paygForm"', html)
        self.assertIn('Volume pricing tiers', html)
        self.assertIn('id="emailCount"', html)

    def test_pricing_page_does_not_expose_a_per_credit_rate_on_the_new_cards(self):
        r = self.client.get('/pricing/')
        self.assertNotIn('per_credit', r.content.decode().lower())

    def test_standalone_subscription_page_still_has_the_purchase_cards(self):
        self._login()
        r = self.client.get('/subscription/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="scTotal"', html)
        # i_subscription.html has no tab bar / panel wrapper of its own.
        self.assertNotIn('id="panel-subscription"', html)
        self.assertNotIn('id="panel-payg"', html)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SubscriptionCardsLayoutTests(TestCase):
    """The two-column layout, reused verbatim from PAYG's own CSS classes
    rather than duplicated values, on both pages that show the cards."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_pricing_subscription_tab_uses_the_two_column_payg_classes(self):
        r = self.client.get('/pricing/')
        html = r.content.decode()
        # payg-wrap appears twice: once for the original PAYG tab, once for
        # the new Subscription-tab two-column layout. Exact-match on the
        # attribute (not a bare substring count) so this can't be thrown off
        # by the explanatory HTML comment above the panel, which also
        # mentions ".payg-wrap"/".payg-card" as plain text.
        self.assertEqual(html.count('class="payg-wrap"'), 2)
        # Exactly the two new Subscription-tab columns: the original PAYG
        # card also carries a "fade-up" class, so it renders as
        # class="payg-card fade-up" and is not counted by this exact match.
        self.assertEqual(html.count('class="payg-card"'), 2)
        # The old single-card wrapper is gone from the subscription markup.
        self.assertNotIn('class="sc-page"', html)
        self.assertNotIn('class="sc-card"', html)
        self.assertNotIn('sc-divider', html)

    def test_standalone_subscription_page_uses_the_two_column_payg_classes(self):
        r = self.client.get('/subscription/')
        html = r.content.decode()
        self.assertIn('<div class="payg-wrap">', html)
        self.assertEqual(html.count('class="payg-card"'), 2)
        self.assertNotIn('class="sc-page"', html)
        self.assertNotIn('class="sc-card"', html)

    def test_both_pages_still_carry_all_seven_service_rows_after_restyle(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            for service in ('email_validation', 'email_marketing', 'sales_outreach',
                            'reputation', 'header_analysis', 'ip_blocklist',
                            'domain_blocklist'):
                self.assertIn(f'data-service="{service}"', html, f"{url} / {service}")


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SubscriptionPageChromeTests(TestCase):
    """i_subscription.html should look like i_pricing.html (notice bar, hero,
    footer) but without the sidebar; i_pricing.html and every other page must
    keep showing their sidebar exactly as before."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_subscription_page_has_the_notice_bar_hero_and_footer(self):
        html = self.client.get('/subscription/').content.decode()
        self.assertIn('Every verification gives you a clear result', html)
        self.assertIn('<h1>Simple, transparent pricing</h1>', html)
        self.assertIn('class="footer"', html)
        self.assertIn('Waytoinbox — All rights reserved', html)

    def test_subscription_page_has_no_payg_tab_toggle(self):
        html = self.client.get('/subscription/').content.decode()
        self.assertNotIn('pricing-toggle', html)
        self.assertNotIn("switchPricingTab", html)

    def test_subscription_page_body_is_marked_no_sidebar(self):
        html = self.client.get('/subscription/').content.decode()
        self.assertIn('<body class="no-sidebar">', html)

    def test_pricing_page_body_keeps_the_sidebar(self):
        html = self.client.get('/pricing/').content.decode()
        self.assertIn('<body class="">', html)
        self.assertNotIn('no-sidebar', html)

    def test_sidebar_markup_is_untouched_on_both_pages(self):
        # The sidebar element itself must still exist in the DOM everywhere
        # (it is hidden with CSS, not removed), so its own JS never runs
        # against a missing element.
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('<aside class="sidebar" id="sidebar">', html, url)

    def test_dashboard_page_is_unaffected_by_the_new_body_class_hook(self):
        """A page with no body_class override must render an ordinary body
        tag, proving the hook is additive and does not touch other pages."""
        user = make_user('chrome_regression@example.com')
        session = self.client.session
        session['logged_in'] = user.user_email
        session.save()
        r = self.client.get('/dashboard/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('<body class="">', html)
        self.assertNotIn('no-sidebar', html)
