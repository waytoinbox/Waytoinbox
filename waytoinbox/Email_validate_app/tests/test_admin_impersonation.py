"""Admin "Login as User" impersonation.

Verifies the whole flow described in utils.py::get_active_user's
impersonate_as_email branch and views/admin/impersonation.py: starting an
impersonation session never touches session['logged_in']/['is_admin'] (so
admin_required keeps resolving the TRUE admin throughout, and there is
nothing to "restore" on exit beyond popping one session key), every other
view picks up the impersonated identity automatically via
get_active_user()/get_user_id(), an admin can never impersonate another
admin or themselves, the two-actor audit trail (ImpersonationLog +
AdminActivity) is written correctly on both start and end, the exit-
impersonation UI banner (nav_is_impersonating) is mutually exclusive with
the unrelated Main<->Sub Account "Return to Main Account" UI, CSRF
protection is not accidentally bypassed, and a plain /logout/ mid-session
still closes out the audit row.
"""
import json

from django.test import TestCase, Client, RequestFactory, override_settings

from Email_validate_app.models import UserTable, ImpersonationLog, AdminActivity
from Email_validate_app.utils import get_user_id, get_active_user, get_true_user


def make_user(email, verified=True, is_admin=False, is_active=True, parent=None):
    user = UserTable.objects.create_user(
        user_name='Impersonation Test', user_email=email, password='StrongPass123!')
    user.is_verified = verified
    user.is_admin = is_admin
    user.is_active = is_active
    if parent is not None:
        user.parent_account = parent
    user.save()
    return user


def _client_for(email, is_admin=False):
    c = Client(SERVER_NAME='127.0.0.1')
    session = c.session
    session['logged_in'] = email
    if is_admin:
        session['is_admin'] = True
    session.save()
    return c


def _resolve_id(client):
    request = RequestFactory().get('/')
    request.session = client.session
    return get_user_id(request)


_BASE_SETTINGS = dict(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)


@override_settings(**_BASE_SETTINGS)
class ImpersonationPermissionTests(TestCase):
    def setUp(self):
        self.admin = make_user('admin@wti.com', is_admin=True)
        self.target = make_user('user@wti.com')

    def test_unauthenticated_redirected_to_login(self):
        c = Client(SERVER_NAME='127.0.0.1')
        r = c.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.url)

    def test_non_admin_redirected_to_home(self):
        c = _client_for(self.target.user_email)  # is_admin session flag not set
        r = c.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, '/')

    def test_session_flag_alone_is_not_enough_without_db_is_admin(self):
        """admin_required re-verifies is_admin against the DB every request
        -- a tampered/stale session flag with no matching DB row must not
        grant access."""
        plain = make_user('plain@wti.com', is_admin=False)
        c = _client_for(plain.user_email, is_admin=True)
        r = c.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, '/')

    def test_cannot_impersonate_another_admin(self):
        other_admin = make_user('other-admin@wti.com', is_admin=True)
        c = _client_for(self.admin.user_email, is_admin=True)
        r = c.post(f'/wti-admin/users/{other_admin.pk}/impersonate/')
        self.assertEqual(r.json()['status'], 'error')
        self.assertNotIn('impersonate_as_email', c.session)
        self.assertEqual(ImpersonationLog.objects.count(), 0)

    def test_cannot_impersonate_self(self):
        c = _client_for(self.admin.user_email, is_admin=True)
        r = c.post(f'/wti-admin/users/{self.admin.pk}/impersonate/')
        self.assertEqual(r.json()['status'], 'error')
        self.assertNotIn('impersonate_as_email', c.session)


@override_settings(**_BASE_SETTINGS)
class ImpersonationFlowTests(TestCase):
    def setUp(self):
        self.admin = make_user('admin@wti.com', is_admin=True)
        self.target = make_user('target@wti.com')
        self.client_admin = _client_for(self.admin.user_email, is_admin=True)

    def test_start_impersonation_sets_override_without_touching_true_identity(self):
        r = self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        data = r.json()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['data']['redirect'], '/dashboard/')

        session = self.client_admin.session
        self.assertEqual(session['impersonate_as_email'], self.target.user_email)
        # The true admin identity is never displaced -- this is what makes
        # admin_required keep working and "exit" a plain key pop.
        self.assertEqual(session['logged_in'], self.admin.user_email)
        self.assertTrue(session['is_admin'])

    def test_active_identity_resolves_to_target_everywhere(self):
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')

        request = RequestFactory().get('/')
        request.session = self.client_admin.session
        self.assertEqual(get_true_user(request).id, self.admin.id)
        self.assertEqual(get_active_user(request).id, self.target.id)
        self.assertEqual(get_user_id(request), self.target.id)

    def test_dashboard_renders_as_target_with_banner(self):
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        r = self.client_admin.get('/dashboard/')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        # Shared header pill (templates/i_index.html), not the old
        # page-wide banner -- appears on every page, not just /dashboard/.
        self.assertIn('dash-impersonation-pill', body)
        self.assertIn(self.target.user_email, body)
        self.assertIn('exitImpersonationBtn', body)
        # Mutually exclusive with the unrelated Sub-Account switcher UI.
        self.assertNotIn('Return to Main Account', body)

    def test_banner_absent_when_not_impersonating(self):
        c = _client_for(self.target.user_email)
        r = c.get('/dashboard/')
        self.assertNotIn('dash-impersonation-pill', r.content.decode())

    def test_impersonation_log_and_audit_trail_written_on_start(self):
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')

        log = ImpersonationLog.objects.get()
        self.assertEqual(log.admin_id, self.admin.id)
        self.assertEqual(log.target_id, self.target.id)
        self.assertEqual(log.target_email, self.target.user_email)
        self.assertEqual(log.status, 'active')
        self.assertIsNone(log.ended_at)
        self.assertIsNotNone(log.started_at)

        activity = AdminActivity.objects.get(action='user.impersonate_start')
        self.assertEqual(activity.admin_id, self.admin.id)
        self.assertEqual(activity.target_repr, self.target.user_email)
        self.assertEqual(activity.status, 'success')

    def test_exit_restores_admin_and_closes_log(self):
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')

        r = self.client_admin.post('/wti-admin/exit-impersonation/')
        data = r.json()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['data']['redirect'], '/wti-admin/')

        session = self.client_admin.session
        self.assertNotIn('impersonate_as_email', session)
        self.assertEqual(session['logged_in'], self.admin.user_email)

        request = RequestFactory().get('/')
        request.session = session
        self.assertEqual(get_active_user(request).id, self.admin.id)

        log = ImpersonationLog.objects.get()
        self.assertEqual(log.status, 'ended')
        self.assertIsNotNone(log.ended_at)

        self.assertTrue(AdminActivity.objects.filter(action='user.impersonate_end').exists())

    def test_admin_can_still_use_wti_admin_while_impersonating(self):
        """logged_in/is_admin were never touched, so admin_required keeps
        granting access to /wti-admin/ pages even mid-impersonation."""
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        r = self.client_admin.get('/wti-admin/users/')
        self.assertEqual(r.status_code, 200)

    def test_admin_panel_pill_shows_immediately_without_a_page_refresh(self):
        """admin_required marks every admin response no-store so the pill
        can never be served stale from the browser's back/forward cache
        after starting impersonation -- the next /wti-admin/ request the
        admin's browser actually makes always reflects current session
        state instead of requiring a manual hard refresh."""
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        r = self.client_admin.get('/wti-admin/users/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get('Cache-Control'), 'no-store')
        body = r.content.decode()
        self.assertIn('admin-impersonation-pill', body)
        self.assertIn(self.target.user_email, body)

    def test_impersonation_pill_appears_on_every_page_not_just_dashboard(self):
        """The pill lives in i_index.html's shared topbar, so it shows on
        any page under that base template, not only /dashboard/."""
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        r = self.client_admin.get('/sub-accounts/')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('dash-impersonation-pill', body)
        self.assertIn(self.target.user_email, body)

    def test_logout_mid_impersonation_closes_open_log(self):
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        r = self.client_admin.get('/logout/')
        self.assertEqual(r.status_code, 302)

        log = ImpersonationLog.objects.get()
        self.assertEqual(log.status, 'ended')
        self.assertIsNotNone(log.ended_at)
        # Plain logout flushes everything, including the true admin login.
        self.assertNotIn('logged_in', self.client_admin.session)

    def test_stale_impersonate_key_for_revoked_admin_falls_back_safely(self):
        """If the admin's own is_admin is revoked mid-session, the next
        request must silently fall back to (the now non-admin) true user,
        never keep exposing the target's data."""
        self.client_admin.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        self.admin.is_admin = False
        self.admin.save()

        request = RequestFactory().get('/')
        request.session = self.client_admin.session
        resolved = get_active_user(request)
        self.assertEqual(resolved.id, self.admin.id)
        self.assertNotIn('impersonate_as_email', request.session)


@override_settings(**_BASE_SETTINGS)
class ImpersonationCsrfTests(TestCase):
    def setUp(self):
        self.admin = make_user('admin@wti.com', is_admin=True)
        self.target = make_user('target@wti.com')

    def test_impersonate_requires_csrf_token(self):
        c = Client(SERVER_NAME='127.0.0.1', enforce_csrf_checks=True)
        session = c.session
        session['logged_in'] = self.admin.user_email
        session['is_admin'] = True
        session.save()

        r = c.post(f'/wti-admin/users/{self.target.pk}/impersonate/')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(ImpersonationLog.objects.exists())

    def test_exit_impersonation_requires_csrf_token(self):
        c = Client(SERVER_NAME='127.0.0.1', enforce_csrf_checks=True)
        session = c.session
        session['logged_in'] = self.admin.user_email
        session['is_admin'] = True
        session['impersonate_as_email'] = self.target.user_email
        session.save()

        r = c.post('/wti-admin/exit-impersonation/')
        self.assertEqual(r.status_code, 403)
