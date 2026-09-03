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
from unittest.mock import patch

from django.test import TestCase, Client, override_settings

from Email_validate_app.models import UserTable


def make_user(email, verified=False):
    user = UserTable.objects.create_user(
        user_name='Pricing Page Test', user_email=email, password='StrongPass123!')
    if verified:
        user.is_verified = True
        user.save(update_fields=['is_verified'])
    return user


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
        # Confirmation is the shared Order Summary modal now, not a
        # subscription-only one — see SubscriptionSharesPaygOrderSummaryTests.
        self.assertIn('id="payConfirmModal"', html)
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


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class PaygPricingTableTests(TestCase):
    """The Pay-As-You-Go volume pricing table, on both pages. This is a
    separate, hardcoded pricing surface from the Subscription cards (not
    backed by CreditPackage) -- its own <table> rows and its own inline
    getPrice()/getRowIndex() JS both need to agree."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_rendered_table_shows_the_new_rates(self):
        rows = [
            ('Up to 10,000',    '$0.0039'),
            ('Up to 50,000',    '$0.00178'),
            ('Up to 100,000',   '$0.00149'),
            ('Up to 500,000',   '$0.00059'),
            ('Up to 1,000,000', '$0.00044'),
            ('2,000,000+',      '$0.00039'),
        ]
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            for label, price in rows:
                self.assertIn(label, html, f"{url}: {label}")
                self.assertIn(price, html, f"{url}: {price}")
            # The old table must be completely gone, not just superseded.
            for old in ('Up to 5,000', '$0.007', '$0.004<', '$0.003<',
                       '$0.002<', '$0.0024', '$0.001<'):
                self.assertNotIn(old, html, f"{url}: stale value {old!r}")

    def test_row_thresholds_match_the_new_boundaries(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('data-max="10000"', html, url)
            self.assertIn('data-min="10000"', html, url)

    def test_calculator_js_uses_the_new_rates(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('10000: 0.0039', html, url)
            self.assertIn('50000: 0.00178', html, url)
            self.assertIn('100000: 0.00149', html, url)
            self.assertIn('500000: 0.00059', html, url)
            self.assertIn('1000000: 0.00044', html, url)
            self.assertIn('2000000: 0.00039', html, url)
            self.assertIn('count <= 10000)', html, url)
            # "count <= 5000)" (with the closing paren) rather than a bare
            # "count <= 5000" substring, which would also match the
            # still-correct, unrelated "count <= 50000)" check.
            self.assertNotIn('count <= 5000)', html, url)


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='rzp_test_secret',
)
class PaygMinimumOrderTests(TestCase):
    """order_payment()'s minimum is now a plain $1.00 floor, full stop --
    the separate 150-credit floor is gone, since at the current per-email
    rates a fixed credit count no longer maps to a fixed dollar amount."""

    def setUp(self):
        self.user = make_user('payg_min@example.com', verified=True)
        self.client = Client(SERVER_NAME='127.0.0.1')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _post(self, credits, price):
        return self.client.post('/pricing/order_payment/', {
            'plan': str(credits), 'price': str(price),
            'pricePerEmail': '0.0039', 'usd-inr': 'USD',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_below_one_dollar_is_rejected(self):
        r = self._post(credits=10, price=0.50)
        self.assertEqual(r.status_code, 400)
        self.assertIn('$1.00', r.json()['message'])
        self.assertNotIn('150', r.json()['message'])

    def test_below_one_dollar_is_rejected_even_with_a_huge_credit_count(self):
        """Proves the floor is purely dollar-based now, not influenced by
        credit count at all -- 100,000 credits was always far above the old
        150-credit floor, but $0.99 must still be rejected."""
        r = self._post(credits=100_000, price=0.99)
        self.assertEqual(r.status_code, 400)
        self.assertIn('$1.00', r.json()['message'])

    def test_at_least_one_dollar_is_accepted_regardless_of_credit_count(self):
        """10 credits is far below the old 150-credit floor -- must now
        succeed purely because the price clears $1.00."""
        with patch('Email_validate_app.views.billing.razorpay.Client') as ClientCls:
            ClientCls.return_value.order.create.return_value = {
                'id': 'order_TEST123', 'amount': 100, 'currency': 'USD',
            }
            r = self._post(credits=10, price=1.00)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['status'], 'ok')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SubscriptionSharesPaygOrderSummaryTests(TestCase):
    """"Get Started" now leads to the exact same "Order Summary" confirmation
    step (#payConfirmModal / openPayConfirm / proceedToPay / openRazorpay) as
    Pay-As-You-Go's "Buy Now", instead of its own separate #scModal."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_old_custom_confirmation_modal_is_gone(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('id="scModal"', html, url)
            self.assertNotIn('scModalConfirm', html, url)
            self.assertNotIn('scModalCancel', html, url)
            self.assertNotIn('function openModal', html, url)
            self.assertNotIn('function closeModal', html, url)

    def test_shared_order_summary_modal_present_on_both_pages(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('id="payConfirmModal"', html, url)
            self.assertIn('Order Summary', html, url)
            self.assertIn('Review your order before proceeding to payment', html, url)
            self.assertIn('function openPayConfirm', html, url)
            self.assertIn('function proceedToPay', html, url)

    def test_open_razorpay_routes_service_credits_to_subscription_verify(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn("isServiceCredits", html, url)
            self.assertIn("order.flow === 'service_credits'", html, url)
            self.assertIn('subscription/verify/', html, url)

    def test_buy_credits_js_no_longer_ships_its_own_modal_wiring(self):
        from django.contrib.staticfiles.finders import find

        js_path = find('Email_validate_app/js/buy_credits.js')
        with open(js_path, encoding='utf-8') as f:
            js = f.read()
        for stale in ('scModal', 'openModal', 'closeModal', 'modalConfirm', 'modalCancel'):
            self.assertNotIn(stale, js, stale)
        # Calls the "Confirm your purchase" / "Order Summary" popup's own
        # open function, not the Pay-As-You-Go-only openPayConfirm.
        self.assertIn('openServiceConfirm', js)
        self.assertNotIn('openPayConfirm', js)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class AllSevenServicesRequiredUiTests(TestCase):
    """"Get Started" is only enabled once every one of the 7 services has a
    quantity at or above its own minimum -- checked in buy_credits.js's
    incompleteServices()/refreshCta(), enforced for real server-side in
    subscription_order() (see test_service_checkout.py /
    AllServicesRequiredTests, test_coupons.py)."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_incomplete_services_gate_shipped_in_buy_credits_js(self):
        from django.contrib.staticfiles.finders import find

        js_path = find('Email_validate_app/js/buy_credits.js')
        with open(js_path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn('function incompleteServices', js)
        self.assertIn('Select credits for every service to continue', js)
        # The old "choose at least one" copy must be gone -- a single
        # service is no longer sufficient.
        self.assertNotIn('at least one service', js)

    def test_static_cta_hint_copy_matches_the_all_seven_rule(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('Select credits for every service to continue.', html, url)
            self.assertNotIn('at least one service', html, url)

    def test_cta_button_starts_disabled_and_cart_gate_uses_all_seven(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('id="scGetStarted" class="sc-cta" disabled', html, url)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class ServiceConfirmPopupTests(TestCase):
    """The "Get Started" -> "Confirm your purchase" / "Order Summary"
    two-column popup: a separate popup from Pay-As-You-Go's own
    #payConfirmModal (which is untouched), styled consistently with it."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_popup_present_with_two_column_headings_on_both_pages(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('id="scConfirmModal"', html, url)
            self.assertIn('pay-confirm-columns', html, url)
            self.assertIn('pay-confirm-box--wide', html, url)
            self.assertIn('<h3>Confirm your purchase</h3>', html, url)
            self.assertIn('id="scConfirmLines"', html, url)

    def test_popup_order_summary_column_has_no_credits_row(self):
        """The literal requirement: Order Summary shows the usual
        Name/Email/Order ID/Discount/Total rows, but never a "Credits" or
        "Plan" row -- that information lives entirely in the "Confirm your
        purchase" column (scConfirmLines) instead."""
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            start = html.index('id="scConfirmModal"')
            popup_html = html[start:start + 4000]
            self.assertIn('id="scPcName"', popup_html, url)
            self.assertIn('id="scPcEmail"', popup_html, url)
            self.assertIn('id="scPcOrderId"', popup_html, url)
            self.assertIn('id="scPcDiscountRow"', popup_html, url)
            self.assertIn('id="scPcAmount"', popup_html, url)
            self.assertNotIn('scPcPlan', popup_html, url)
            self.assertNotIn('>Credits<', popup_html, url)
            self.assertNotIn('>Plan<', popup_html, url)

    def test_popup_reuses_pay_confirm_css_classes(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            start = html.index('id="scConfirmModal"')
            popup_html = html[start:start + 4000]
            for cls in ('pay-confirm-box', 'pay-confirm-icon', 'pay-confirm-sub',
                       'pay-confirm-rows', 'pay-confirm-actions',
                       'pay-confirm-cancel', 'pay-confirm-pay', 'pay-confirm-secure'):
                self.assertIn(cls, popup_html, f"{url}: {cls}")

    def test_two_column_css_rules_exist_and_collapse_on_mobile(self):
        from django.contrib.staticfiles.finders import find

        css_path = find('Email_validate_app/css/components.css')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        self.assertIn('.pay-confirm-columns', css)
        self.assertIn('.pay-confirm-box--wide', css)
        self.assertIn('@media (max-width: 560px)', css)

    def test_open_service_confirm_populates_lines_and_summary_without_credits(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('function openServiceConfirm', html, url)
            self.assertIn('function closeServiceConfirm', html, url)
            self.assertIn('function proceedToServicePay', html, url)
            self.assertIn('scConfirmLines', html, url)
            # PAYG's own popup functions must be completely untouched.
            self.assertIn('function openPayConfirm', html, url)
            self.assertIn('function closePayConfirm', html, url)
            self.assertIn('function proceedToPay', html, url)

    def test_popup_styling_is_tokenized_not_inline(self):
        """The discount color and the discount row's visibility used to be a
        hardcoded inline style (#007A5E, style="display:none") duplicated 4x
        across both modals/both templates -- now a real CSS class plus the
        native `hidden` attribute, matching this codebase's established
        [hidden]-attribute pattern (so_inbox.css, so_campaign.css, etc.)
        rather than JS-driven style.display."""
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            # Scope to the two payment modals themselves (payConfirmModal
            # through the end of scConfirmModal) -- the page elsewhere
            # legitimately uses #007A5E for an unrelated "Free" nav badge
            # (i_index.html), which this test must not false-positive on.
            start = html.index('id="payConfirmModal"')
            modals_html = html[start:start + 3700]
            self.assertNotIn('#007A5E', modals_html, url)
            self.assertNotIn('style="display:none"', modals_html, url)
            self.assertIn('pay-confirm-discount', modals_html, url)
            self.assertIn('id="scPcDiscountRow" hidden', modals_html, url)
            self.assertIn('id="pcDiscountRow" hidden', modals_html, url)
            self.assertIn('discRow.hidden = false', html, url)
            self.assertIn('discRow.hidden = true', html, url)
            self.assertNotIn('discRow.style.display', html, url)

    def test_popup_summary_block_balances_the_two_columns(self):
        """Column 1 always renders exactly 7 rows now (every service is
        required); Discount+Total are grouped into their own block in
        column 2 so it reads as complete rather than sparse next to it."""
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            start = html.index('id="scConfirmModal"')
            popup_html = html[start:start + 4300]
            self.assertIn('pay-confirm-summary-block', popup_html, url)
            self.assertIn('id="scPcDiscountRow"', popup_html, url)
            self.assertIn('id="scPcAmount"', popup_html, url)

    def test_summary_block_css_exists(self):
        from django.contrib.staticfiles.finders import find

        css_path = find('Email_validate_app/css/components.css')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        self.assertIn('.pay-confirm-summary-block', css)
        self.assertIn('.pay-confirm-discount', css)
        self.assertIn('.pay-confirm-row[hidden]', css)
        self.assertIn('margin-top: auto', css)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class TrialPopupTests(TestCase):
    """The 7-Day Free Trial popup on i_pricing.html/i_subscription.html.
    Never activates automatically -- shown only to an eligible session
    (never started a trial), with different content for unverified vs
    verified, and not shown at all once a trial is active or already used."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _login(self, user):
        session = self.client.session
        session['logged_in'] = user.user_email
        session.save()

    def test_no_popup_for_an_anonymous_visitor(self):
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('trialPopupModal', html, url)

    def test_unverified_eligible_user_sees_verify_message_not_activate_button(self):
        user = make_user('trial_popup_unverified@example.com', verified=False)
        self._login(user)
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('id="trialPopupModal"', html, url)
            self.assertIn('Verify Your Email First', html, url)
            self.assertNotIn('id="trialActivateBtn"', html, url)
            self.assertNotIn('function activateTrial', html, url)

    def test_verified_eligible_user_sees_activate_button_and_limits(self):
        user = make_user('trial_popup_verified@example.com', verified=True)
        self._login(user)
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertIn('id="trialPopupModal"', html, url)
            self.assertIn('Activate Your 7-Day Free Trial', html, url)
            self.assertIn('id="trialActivateBtn"', html, url)
            self.assertIn('function activateTrial', html, url)
            self.assertNotIn('Verify Your Email First', html, url)
            start = html.index('id="trialPopupModal"')
            popup_html = html[start:start + 3000]
            # New limits: Email Validation 50, Email Marketing 50,
            # Sales Outreach 1, Reputation 1, Header Analyzer 10,
            # IP Blocklist 1, Domain Blocklist 1.
            self.assertIn('50 emails', popup_html, url)
            self.assertIn('10 headers', popup_html, url)

    def test_activate_button_posts_to_the_trial_activate_endpoint(self):
        user = make_user('trial_popup_endpoint@example.com', verified=True)
        self._login(user)
        html = self.client.get('/pricing/').content.decode()
        self.assertIn("fetch('/trial/activate/'", html)
        self.assertIn('WTICheckout.csrfToken()', html)

    def test_no_popup_once_a_trial_is_active(self):
        from Email_validate_app.services.trial_manager import activate_trial
        user = make_user('trial_popup_active@example.com', verified=True)
        activate_trial(user)
        self._login(user)
        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('trialPopupModal', html, url)

    def test_no_popup_once_a_trial_is_already_used_and_expired(self):
        from datetime import timedelta
        from django.utils.timezone import now
        from Email_validate_app.services.trial_manager import activate_trial

        user = make_user('trial_popup_expired@example.com', verified=True)
        activate_trial(user)
        started = now() - timedelta(days=8)
        user.trial_started_at = started
        user.trial_ends_at = started + timedelta(days=7)
        user.save(update_fields=['trial_started_at', 'trial_ends_at'])
        self._login(user)

        for url in ('/pricing/', '/subscription/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('trialPopupModal', html, url)

    def test_close_button_and_backdrop_click_handler_present(self):
        user = make_user('trial_popup_close@example.com', verified=True)
        self._login(user)
        html = self.client.get('/pricing/').content.decode()
        self.assertIn('function closeTrialPopup', html)
        self.assertIn("addEventListener('click'", html)
