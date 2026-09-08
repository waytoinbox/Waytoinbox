"""Admin-approved free/public email signup requests.

Self-service signup (views/auth.py::signup) still blocks a free/public
domain outright via is_free_email_domain() -- untouched by this feature.
This file covers the one sanctioned exception: a public request
(FreeEmailSignupRequest, status=pending) that only an admin can approve
(views/admin/free_email_requests.py), which is the only code path allowed
to create a UserTable row for such an address, and only after that
explicit decision.
"""
import json

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, Client, override_settings

from Email_validate_app.models import UserTable, FreeEmailSignupRequest, AdminActivity


_BASE_SETTINGS = dict(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)

REQUEST_URL = '/request-free-email-signup/'


def make_user(email, verified=True, is_admin=False, is_active=True):
    user = UserTable.objects.create_user(
        user_name='Test User', user_email=email, password='StrongPass123!')
    user.is_verified = verified
    user.is_admin = is_admin
    user.is_active = is_active
    user.save()
    return user


def _client_for(email, is_admin=False):
    c = Client(SERVER_NAME='127.0.0.1')
    session = c.session
    session['logged_in'] = email
    session['is_admin'] = is_admin
    session.save()
    return c


@override_settings(**_BASE_SETTINGS)
class PublicRequestSubmissionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _submit(self, name='Jane Doe', email='jane@gmail.com', reason='Personal use'):
        return self.client.post(REQUEST_URL, {'name': name, 'email': email, 'reason': reason})

    def test_anonymous_user_can_submit_request(self):
        r = self._submit()
        self.assertEqual(r.json()['status'], 'ok')

    def test_valid_request_creates_row(self):
        self._submit(email='new1@gmail.com')
        self.assertTrue(FreeEmailSignupRequest.objects.filter(email='new1@gmail.com').exists())

    def test_status_defaults_to_pending(self):
        self._submit(email='new2@gmail.com')
        req = FreeEmailSignupRequest.objects.get(email='new2@gmail.com')
        self.assertEqual(req.status, 'pending')

    def test_no_usertable_created(self):
        self._submit(email='new3@gmail.com')
        self.assertFalse(UserTable.objects.filter(user_email='new3@gmail.com').exists())

    def test_malformed_email_rejected(self):
        r = self._submit(email='not-an-email')
        self.assertEqual(r.json()['status'], 'error')
        self.assertFalse(FreeEmailSignupRequest.objects.filter(email='not-an-email').exists())

    def test_existing_user_email_rejected(self):
        make_user('existing@gmail.com')
        r = self._submit(email='existing@gmail.com')
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('already exists', r.json()['message'].lower())
        self.assertFalse(FreeEmailSignupRequest.objects.filter(email='existing@gmail.com').exists())

    def test_duplicate_pending_request_rejected(self):
        self._submit(email='dup@gmail.com')
        r = self._submit(email='dup@gmail.com')
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('already pending', r.json()['message'].lower())
        self.assertEqual(FreeEmailSignupRequest.objects.filter(email='dup@gmail.com').count(), 1)

    def test_rejected_request_allows_a_new_request_later(self):
        self._submit(email='retry@gmail.com')
        req = FreeEmailSignupRequest.objects.get(email='retry@gmail.com')
        req.status = 'rejected'
        req.save(update_fields=['status'])

        r = self._submit(email='retry@gmail.com')
        self.assertEqual(r.json()['status'], 'ok')
        self.assertEqual(FreeEmailSignupRequest.objects.filter(email='retry@gmail.com').count(), 2)

    def test_rate_limiting(self):
        for i in range(5):
            self._submit(email=f'rl{i}@gmail.com')
        r = self._submit(email='rl-over@gmail.com')
        self.assertEqual(r.status_code, 429)

    def test_admin_notification_triggered(self):
        mail.outbox = []
        self._submit(email='notify@gmail.com', name='Notify Me')
        self.assertTrue(any('notify@gmail.com' in m.subject for m in mail.outbox))


@override_settings(**_BASE_SETTINGS)
class AdminAccessControlTests(TestCase):
    def setUp(self):
        self.normal_user = make_user('normal@company.com')
        self.admin_user = make_user('admin1@company.com', is_admin=True)
        self.req = FreeEmailSignupRequest.objects.create(name='X', email='x@gmail.com')

    def test_anonymous_cannot_view_list(self):
        r = Client(SERVER_NAME='127.0.0.1').get('/wti-admin/free-email-requests/')
        self.assertNotEqual(r.status_code, 200)

    def test_normal_user_cannot_view_list(self):
        c = _client_for(self.normal_user.user_email, is_admin=False)
        r = c.get('/wti-admin/free-email-requests/')
        self.assertNotEqual(r.status_code, 200)

    def test_anonymous_cannot_approve(self):
        r = Client(SERVER_NAME='127.0.0.1').post(f'/wti-admin/free-email-requests/{self.req.pk}/approve/')
        self.assertNotEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_normal_user_cannot_approve(self):
        c = _client_for(self.normal_user.user_email, is_admin=False)
        r = c.post(f'/wti-admin/free-email-requests/{self.req.pk}/approve/')
        self.assertNotEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_anonymous_cannot_reject(self):
        r = Client(SERVER_NAME='127.0.0.1').post(
            f'/wti-admin/free-email-requests/{self.req.pk}/reject/', {'reason': 'no'})
        self.assertNotEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_normal_user_cannot_reject(self):
        c = _client_for(self.normal_user.user_email, is_admin=False)
        r = c.post(f'/wti-admin/free-email-requests/{self.req.pk}/reject/', {'reason': 'no'})
        self.assertNotEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_admin_can_view_list(self):
        c = _client_for(self.admin_user.user_email, is_admin=True)
        r = c.get('/wti-admin/free-email-requests/')
        self.assertEqual(r.status_code, 200)

    def test_admin_can_view_detail(self):
        c = _client_for(self.admin_user.user_email, is_admin=True)
        r = c.get(f'/wti-admin/free-email-requests/{self.req.pk}/')
        self.assertEqual(r.status_code, 200)


@override_settings(**_BASE_SETTINGS)
class ApprovalTests(TestCase):
    def setUp(self):
        self.admin_user = make_user('admin2@company.com', is_admin=True)
        self.client = _client_for(self.admin_user.user_email, is_admin=True)
        self.req = FreeEmailSignupRequest.objects.create(
            name='Approve Me', email='approveme@gmail.com', reason='need it')

    def _approve(self, rid=None):
        return self.client.post(f'/wti-admin/free-email-requests/{rid or self.req.pk}/approve/')

    def test_approval_creates_usertable(self):
        self._approve()
        self.assertTrue(UserTable.objects.filter(user_email='approveme@gmail.com').exists())

    def test_approved_account_has_unusable_password_initially(self):
        self._approve()
        user = UserTable.objects.get(user_email='approveme@gmail.com')
        self.assertFalse(user.has_usable_password())

    def test_approved_account_is_verified(self):
        self._approve()
        user = UserTable.objects.get(user_email='approveme@gmail.com')
        self.assertTrue(user.is_verified)

    def test_approved_account_is_a_normal_main_account(self):
        self._approve()
        user = UserTable.objects.get(user_email='approveme@gmail.com')
        self.assertIsNone(user.parent_account_id)
        self.assertFalse(user.is_admin)

    def test_free_email_restriction_does_not_block_admin_creation(self):
        # The account is created directly by the service, never through
        # signup()/is_free_email_domain() -- this simply proves the row
        # exists despite being a gmail.com address.
        self._approve()
        self.assertTrue(UserTable.objects.filter(user_email='approveme@gmail.com').exists())

    def test_password_setup_token_created(self):
        self._approve()
        user = UserTable.objects.get(user_email='approveme@gmail.com')
        self.assertIsNotNone(user.reset_token)
        self.assertIsNotNone(user.reset_token_expiry)

    def test_password_setup_email_triggered(self):
        mail.outbox = []
        self._approve()
        self.assertTrue(any('approved' in m.subject.lower() for m in mail.outbox))

    def test_request_becomes_approved(self):
        self._approve()
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'approved')

    def test_reviewed_by_stored(self):
        self._approve()
        self.req.refresh_from_db()
        self.assertEqual(self.req.reviewed_by_id, self.admin_user.id)

    def test_reviewed_at_stored(self):
        self._approve()
        self.req.refresh_from_db()
        self.assertIsNotNone(self.req.reviewed_at)

    def test_created_user_stored(self):
        self._approve()
        self.req.refresh_from_db()
        user = UserTable.objects.get(user_email='approveme@gmail.com')
        self.assertEqual(self.req.created_user_id, user.id)

    def test_admin_activity_recorded(self):
        self._approve()
        self.assertTrue(AdminActivity.objects.filter(action='free_email_request.approve').exists())

    def test_approving_already_existing_email_fails_safely(self):
        make_user('approveme@gmail.com')
        r = self._approve()
        body = r.json()
        self.assertEqual(body['status'], 'error')
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')
        # Only the one pre-existing account -- no duplicate/second row.
        self.assertEqual(UserTable.objects.filter(user_email='approveme@gmail.com').count(), 1)

    def test_approving_already_approved_request_fails(self):
        self._approve()
        r2 = self._approve()
        self.assertEqual(r2.json()['status'], 'error')
        self.assertEqual(UserTable.objects.filter(user_email='approveme@gmail.com').count(), 1)

    def test_approving_already_rejected_request_fails(self):
        self.client.post(f'/wti-admin/free-email-requests/{self.req.pk}/reject/', {'reason': 'no thanks'})
        r = self._approve()
        self.assertEqual(r.json()['status'], 'error')
        self.assertFalse(UserTable.objects.filter(user_email='approveme@gmail.com').exists())


@override_settings(**_BASE_SETTINGS)
class RejectionTests(TestCase):
    def setUp(self):
        self.admin_user = make_user('admin3@company.com', is_admin=True)
        self.client = _client_for(self.admin_user.user_email, is_admin=True)
        self.req = FreeEmailSignupRequest.objects.create(name='Reject Me', email='rejectme@gmail.com')

    def _reject(self, reason='Not a good fit', rid=None):
        return self.client.post(f'/wti-admin/free-email-requests/{rid or self.req.pk}/reject/', {'reason': reason})

    def test_rejection_requires_a_reason(self):
        r = self._reject(reason='')
        self.assertEqual(r.json()['status'], 'error')
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_rejection_stores_reason(self):
        self._reject(reason='Suspicious request')
        self.req.refresh_from_db()
        self.assertEqual(self.req.rejection_reason, 'Suspicious request')

    def test_request_becomes_rejected(self):
        self._reject()
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'rejected')

    def test_reviewed_by_and_at_stored(self):
        self._reject()
        self.req.refresh_from_db()
        self.assertEqual(self.req.reviewed_by_id, self.admin_user.id)
        self.assertIsNotNone(self.req.reviewed_at)

    def test_no_usertable_created(self):
        self._reject()
        self.assertFalse(UserTable.objects.filter(user_email='rejectme@gmail.com').exists())

    def test_admin_activity_recorded(self):
        self._reject()
        self.assertTrue(AdminActivity.objects.filter(action='free_email_request.reject').exists())

    def test_second_rejection_attempt_fails(self):
        self._reject()
        r2 = self._reject()
        self.assertEqual(r2.json()['status'], 'error')


@override_settings(**_BASE_SETTINGS)
class RegressionTests(TestCase):
    """Confirms this feature changes nothing about the existing block or
    Contact Us -- reuses the exact assertions test_signup_business_email.py
    already established as the baseline."""

    def setUp(self):
        cache.clear()
        self.client = Client(SERVER_NAME='127.0.0.1')

    def _signup(self, email, name='Reg Test', password='StrongPass123!'):
        return self.client.post('/signup/', {
            'user_name': name, 'user_email': email,
            'password': password, 'confirm_password': password,
        })

    def test_gmail_signup_still_blocked(self):
        body = self._signup('reg1@gmail.com').json()
        self.assertEqual(body['status'], 'error')
        self.assertFalse(UserTable.objects.filter(user_email='reg1@gmail.com').exists())

    def test_outlook_signup_still_blocked(self):
        body = self._signup('reg2@outlook.com').json()
        self.assertEqual(body['status'], 'error')

    def test_yahoo_signup_still_blocked(self):
        body = self._signup('reg3@yahoo.com').json()
        self.assertEqual(body['status'], 'error')

    def test_business_email_signup_still_allowed(self):
        body = self._signup('reg4@company.com').json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(UserTable.objects.filter(user_email='reg4@company.com').exists())

    def test_existing_free_email_account_login_unaffected(self):
        user = make_user('reg5@gmail.com')
        r = self.client.post('/login/', {'email': 'reg5@gmail.com', 'password': 'StrongPass123!'})
        self.assertEqual(r.json()['status'], 'ok')

    def test_existing_unverified_free_email_resend_unaffected(self):
        make_user('reg6@gmail.com', verified=False)
        body = self._signup('reg6@gmail.com', name='Reg Test').json()
        self.assertEqual(body['status'], 'info')
        self.assertEqual(UserTable.objects.filter(user_email='reg6@gmail.com').count(), 1)

    def test_contact_us_unaffected(self):
        r = self.client.post('/Contact_Us/', {
            'name': 'Someone', 'email': 'someone@gmail.com', 'message': 'hello',
        })
        self.assertEqual(r.json()['status'], 'success')
