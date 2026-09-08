"""Business-email-only self-service signup restriction.

Covers Email_validate_app.services.email_domain_policy.is_free_email_domain
(pure unit tests) and its wiring into views.auth.signup() (integration
tests), plus the anonymous-Contact-Us change in views.billing.contact_us()
that the blocked-signup message links to. Confirms existing accounts,
login, forgot-password/reset-password, the unverified-resend path, and
business-domain verification are all completely unaffected.
"""
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from datetime import timedelta

from Email_validate_app.models import UserTable
from Email_validate_app.services.email_domain_policy import (
    is_free_email_domain, FREE_EMAIL_DOMAINS,
)


class IsFreeEmailDomainTests(TestCase):
    """Pure unit tests for the centralized helper -- no DB/client needed."""

    def test_business_domain_allowed(self):
        self.assertFalse(is_free_email_domain('user@company.com'))

    def test_gmail_blocked(self):
        self.assertTrue(is_free_email_domain('user@gmail.com'))

    def test_outlook_blocked(self):
        self.assertTrue(is_free_email_domain('user@outlook.com'))

    def test_hotmail_blocked(self):
        self.assertTrue(is_free_email_domain('user@hotmail.com'))

    def test_yahoo_blocked(self):
        self.assertTrue(is_free_email_domain('user@yahoo.com'))

    def test_all_listed_provider_aliases_blocked(self):
        for domain in FREE_EMAIL_DOMAINS:
            self.assertTrue(is_free_email_domain(f'user@{domain}'), domain)

    def test_mixed_case_domain_blocked(self):
        self.assertTrue(is_free_email_domain('user@GMAIL.COM'))
        self.assertTrue(is_free_email_domain('user@Yahoo.Co.In'))

    def test_surrounding_whitespace_handled(self):
        self.assertTrue(is_free_email_domain('user@gmail.com   '))
        self.assertFalse(is_free_email_domain('user@company.com  '))

    def test_exact_match_only_no_substring_false_positive(self):
        # These contain a blocked name as a substring but must NOT match.
        self.assertFalse(is_free_email_domain('user@notgmail.com'))
        self.assertFalse(is_free_email_domain('user@gmail.com.evil.com'))
        self.assertFalse(is_free_email_domain('user@mycompany-outlook.com'))

    def test_empty_or_malformed_input_is_safe(self):
        self.assertFalse(is_free_email_domain(''))
        self.assertFalse(is_free_email_domain('not-an-email'))
        self.assertFalse(is_free_email_domain(None))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class SignupBusinessEmailIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        cache.clear()

    def _signup(self, email, name='Test User', password='StrongPass123!'):
        return self.client.post('/signup/', {
            'user_name': name,
            'user_email': email,
            'password': password,
            'confirm_password': password,
        })

    def test_business_email_allowed(self):
        resp = self._signup('user@company.com')
        data = resp.json()
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(UserTable.objects.filter(user_email='user@company.com').exists())

    def test_gmail_blocked(self):
        data = self._signup('user@gmail.com').json()
        self.assertEqual(data['status'], 'error')

    def test_outlook_blocked(self):
        data = self._signup('user@outlook.com').json()
        self.assertEqual(data['status'], 'error')

    def test_hotmail_blocked(self):
        data = self._signup('user@hotmail.com').json()
        self.assertEqual(data['status'], 'error')

    def test_yahoo_blocked(self):
        data = self._signup('user@yahoo.com').json()
        self.assertEqual(data['status'], 'error')

    def test_mixed_case_domain_blocked(self):
        data = self._signup('user@GMAIL.COM').json()
        self.assertEqual(data['status'], 'error')
        self.assertFalse(UserTable.objects.filter(user_email__iexact='user@GMAIL.COM').exists())

    def test_business_email_with_surrounding_whitespace_allowed(self):
        # The view already strips the raw POST value before any checks.
        resp = self.client.post('/signup/', {
            'user_name': 'Whitespace User',
            'user_email': '  wsuser@company.com  ',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        data = resp.json()
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(UserTable.objects.filter(user_email='wsuser@company.com').exists())

    def test_blocked_signup_creates_no_user_row(self):
        self._signup('nouser@yahoo.com')
        self.assertFalse(UserTable.objects.filter(user_email='nouser@yahoo.com').exists())

    def test_blocked_signup_error_message_explains_and_links_request_approval(self):
        # Message wording changed from a generic "Contact us" link to a
        # dedicated "request approval" action pointing at the new
        # admin-approved free-email signup flow (see
        # views/auth.py::request_free_email_signup and
        # test_free_email_signup_requests.py) -- the underlying block
        # itself is unchanged, only tested here via the same assertions.
        data = self._signup('user@icloud.com').json()
        message = data['message'].lower()
        self.assertIn('business email', message)
        self.assertIn('request approval', message)

    def test_business_email_verification_flow_still_works(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        self._signup('newbiz@company.com')
        user = UserTable.objects.get(user_email='newbiz@company.com')
        self.assertFalse(user.is_verified)

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        self.client.get(f'/verify/{uid}/{token}/')

        user.refresh_from_db()
        self.assertTrue(user.is_verified)


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class ExistingFreeEmailAccountUnaffectedTests(TestCase):
    """The restriction must never apply retroactively to rows that already
    exist -- login, forgot-password and reset-password must all keep
    working exactly as before for a pre-existing @gmail.com account."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        cache.clear()
        self.password = 'StrongPass123!'
        self.user = UserTable.objects.create_user(
            user_name='Existing Gmail User',
            user_email='existing@gmail.com',
            password=self.password,
        )
        self.user.is_verified = True
        self.user.save()

    def test_login_still_works(self):
        resp = self.client.post('/login/', {'email': 'existing@gmail.com', 'password': self.password})
        data = resp.json()
        self.assertEqual(data['status'], 'ok')

    def test_forgot_password_still_works(self):
        resp = self.client.post('/forgot-password/', {'email': 'existing@gmail.com'})
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.reset_token)

    def test_password_reset_still_works(self):
        self.user.reset_token = 'existing-gmail-reset-token'
        self.user.reset_token_expiry = timezone.now() + timedelta(hours=1)
        self.user.save()

        resp = self.client.post(
            '/reset-password/existing-gmail-reset-token/',
            {'new_password': 'NewStrongPass456!'},
        )
        data = resp.json()
        self.assertEqual(data['status'], 'ok')

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewStrongPass456!'))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class ExistingUnverifiedFreeEmailResendTests(TestCase):
    """A pre-existing, still-unverified @gmail.com account resubmitting the
    signup form must keep getting the exact same "resend verification"
    behavior as before -- it is not a new account, so the new domain gate
    (which only runs when no existing row is found) must never see it."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        cache.clear()
        self.user = UserTable.objects.create_user(
            user_name='Pending Gmail User',
            user_email='pending@gmail.com',
            password='StrongPass123!',
        )

    def test_resend_verification_unchanged(self):
        resp = self.client.post('/signup/', {
            'user_name': 'Pending Gmail User',
            'user_email': 'pending@gmail.com',
            'password': 'StrongPass123!',
            'confirm_password': 'StrongPass123!',
        })
        data = resp.json()
        self.assertEqual(data['status'], 'info')
        self.assertIn('verification email', data['message'].lower())
        self.assertEqual(UserTable.objects.filter(user_email='pending@gmail.com').count(), 1)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ContactUsAnonymousTests(TestCase):
    """A visitor blocked at signup has no session -- Contact Us must work
    for them without logging in, while the pre-existing logged-in path
    keeps working exactly as before."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')

    def test_anonymous_submission_succeeds(self):
        resp = self.client.post('/Contact_Us/', {
            'name': 'Blocked Visitor',
            'email': 'visitor@gmail.com',
            'message': 'I need to sign up with my personal email, please help.',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'success')

    def test_logged_in_submission_still_works(self):
        user = UserTable.objects.create_user(
            user_name='Logged In User',
            user_email='loggedin@company.com',
            password='StrongPass123!',
        )
        user.is_verified = True
        user.save()
        session = self.client.session
        session['logged_in'] = user.user_email
        session.save()

        resp = self.client.post('/Contact_Us/', {
            'name': 'Logged In User',
            'email': 'loggedin@company.com',
            'message': 'Question about billing.',
        })
        self.assertEqual(resp.json()['status'], 'success')
