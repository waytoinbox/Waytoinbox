"""Tests for Phase 7 follow-up: embedding the new service-credit purchase
cards into the old i_pricing.html page's Subscription tab, in place of the
Classic/Standard/Advanced plan cards, while leaving everything else on that
page (and the standalone i_subscription.html page) unchanged.

Covers:
  * /pricing/ still renders for both anonymous and logged-in users.
  * The Subscription tab now contains the service-credit cards (steppers,
    quote/order/verify URLs, all seven service rows) instead of the old
    Classic/Standard/Advanced plan cards, which are gone.
  * The Subscription tab appears before Pay-As-You-Go in the rendered HTML.
  * The Pay-As-You-Go panel itself is untouched.
  * /subscription/ (the standalone page) still renders unchanged.
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

    def test_standalone_subscription_page_still_renders_unchanged(self):
        self._login()
        r = self.client.get('/subscription/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="scTotal"', html)
        # i_subscription.html has no tab bar / panel wrapper of its own.
        self.assertNotIn('id="panel-subscription"', html)
        self.assertNotIn('id="panel-payg"', html)
