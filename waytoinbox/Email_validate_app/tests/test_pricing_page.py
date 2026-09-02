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
        # i_subscription.html now mirrors i_pricing.html's full tab structure
        # (see SubscriptionPageChromeTests), so both panels exist here too.
        self.assertIn('id="panel-subscription"', html)
        self.assertIn('id="panel-payg"', html)


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
        # The left ("Choose your credits") column, bare payg-card; the
        # original PAYG card also carries a "fade-up" class, so it renders
        # as class="payg-card fade-up" and is not counted by this exact match.
        self.assertEqual(html.count('class="payg-card"'), 1)
        # The right ("Your total") column additionally carries the sticky
        # class (see SubscriptionStickyTotalTests).
        self.assertEqual(html.count('class="payg-card sc-summary-card"'), 1)
        # The old single-card wrapper is gone from the subscription markup.
        self.assertNotIn('class="sc-page"', html)
        self.assertNotIn('class="sc-card"', html)
        self.assertNotIn('sc-divider', html)

    def test_standalone_subscription_page_uses_the_two_column_payg_classes(self):
        r = self.client.get('/subscription/')
        html = r.content.decode()
        # Once for the PAYG tab, once for the Subscription tab's two-column
        # layout — same as i_pricing.html.
        self.assertEqual(html.count('class="payg-wrap"'), 2)
        self.assertEqual(html.count('class="payg-card"'), 1)
        self.assertEqual(html.count('class="payg-card sc-summary-card"'), 1)
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
    """i_subscription.html should look and behave exactly like i_pricing.html
    (notice bar, hero, both tabs including Pay-As-You-Go, footer) with the
    one deliberate difference being no sidebar; i_pricing.html and every
    other page must keep showing their sidebar exactly as before."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_subscription_page_has_the_notice_bar_hero_and_footer(self):
        html = self.client.get('/subscription/').content.decode()
        self.assertIn('Every verification gives you a clear result', html)
        self.assertIn('<h1>Simple, transparent pricing</h1>', html)
        self.assertIn('class="footer"', html)
        self.assertIn('Waytoinbox — All rights reserved', html)

    def test_subscription_page_has_the_same_payg_tab_as_pricing(self):
        html = self.client.get('/subscription/').content.decode()
        self.assertIn('pricing-toggle', html)
        self.assertIn("switchPricingTab('subscription'", html)
        self.assertIn("switchPricingTab('payg'", html)
        self.assertIn('How many emails do you have?', html)
        self.assertIn('id="paygForm"', html)
        self.assertIn('Volume pricing tiers', html)
        self.assertIn('id="emailCount"', html)
        # Same tab order and default-active tab as i_pricing.html.
        sub_pos  = html.index("switchPricingTab('subscription'")
        payg_pos = html.index("switchPricingTab('payg'")
        self.assertLess(sub_pos, payg_pos)
        self.assertIn('class="pricing-panel active" id="panel-subscription"', html)
        self.assertIn('class="pricing-panel" id="panel-payg"', html)

    def test_subscription_page_has_the_payg_checkout_infrastructure(self):
        """The PAYG (and legacy-plan) checkout flow needs its own supporting
        elements/JS, which the old sidebar-only page never needed to bring in
        on its own — confirms they were ported, not just the visible tab."""
        html = self.client.get('/subscription/').content.decode()
        self.assertIn('id="sub-alert-area"', html)
        self.assertIn('id="payConfirmModal"', html)
        self.assertIn('function openRazorpay(order)', html)
        self.assertIn('function openPayConfirm(order)', html)
        self.assertIn('order_payment', html)

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


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SubscriptionStickyTotalTests(TestCase):
    """The "Your total" card stays visible on desktop while the (potentially
    taller) "Choose your credits" column scrolls past it, without touching
    the two-column layout, spacing or the PAYG tab's own cards."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_only_the_total_card_carries_the_sticky_class_on_both_pages(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            # Exactly one card is marked sticky: the right ("Your total")
            # column. The left ("Choose your credits") column and both
            # PAYG-tab cards keep their plain, unmodified classes.
            self.assertEqual(html.count('sc-summary-card'), 1, url)
            self.assertIn('class="payg-card sc-summary-card"', html, url)

    def test_sticky_css_rule_exists_and_is_disabled_below_the_payg_wrap_breakpoint(self):
        from django.contrib.staticfiles.finders import find

        css_path = find('Email_validate_app/css/subscription_credits.css')
        self.assertIsNotNone(css_path, 'subscription_credits.css not found by staticfiles finders')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        self.assertIn('.sc-summary-card', css)
        self.assertIn('position: sticky', css)
        self.assertIn('top: calc(var(--nav-h)', css)
        # Disabled at the same breakpoint where .payg-wrap itself collapses
        # to one column (layout-nav-hero.css), so mobile scrolls normally.
        self.assertIn('@media (max-width: 860px)', css)
        self.assertIn('.sc-summary-card { position: static; }', css)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SubscriptionTotalPeriodLabelTests(TestCase):
    """The small "/mo" label beside the total, on both pages. It must be a
    sibling of #scTotal, never nested inside it — buy_credits.js replaces
    #scTotal's entire textContent on every price update
    (setTotal: totalEl.textContent = money(cents)), which would silently
    delete anything nested inside it on the very first quote response."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_period_label_present_on_both_pages(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('class="sc-total-period"', html, url)
            self.assertIn('>/mo<', html, url)

    def test_period_label_is_a_sibling_of_sctotal_not_nested_inside_it(self):
        import re
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            # #scTotal's own element must close (</div>) before the "/mo"
            # span opens -- i.e. sc-total-period is not inside its tag.
            m = re.search(r'<div id="scTotal".*?</div>\s*<span class="sc-total-period">/mo</span>',
                          html, re.S)
            self.assertIsNotNone(m, f"{url}: /mo label must follow #scTotal's closing tag, not be nested inside it")

    def test_period_label_css_rule_exists(self):
        from django.contrib.staticfiles.finders import find

        css_path = find('Email_validate_app/css/subscription_credits.css')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        self.assertIn('.sc-total-row', css)
        self.assertIn('.sc-total-period', css)
