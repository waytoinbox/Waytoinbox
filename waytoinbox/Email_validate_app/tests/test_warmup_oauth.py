"""Tests for the Warmup Receiver Gmail OAuth start/callback views
(Email_validate_app/views/admin/warmup.py), covering the PKCE code_verifier
fix: the callback builds a separate Flow instance from the start view, so
the verifier generated during authorization_url() must be persisted in the
session and threaded back into the callback's Flow, or Google's token
endpoint rejects the exchange with invalid_grant: "Missing code verifier".

All Google API calls are mocked — these tests never make real network
requests to Google.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from Email_validate_app.models import UserTable, WarmupReceiverAccount

WARMUP_MODULE = 'Email_validate_app.views.admin.warmup'
FAKE_OAUTH_CONFIG = ('fake-client-id', 'fake-client-secret',
                     'https://example.com/wti-admin/warmup-receivers/oauth/callback/')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class _AdminSessionTestCase(TestCase):
    """Shared setup — an is_admin=True user, logged in via this app's own
    session-based auth (not Django's own login()), matching admin_required's
    actual checks."""

    def setUp(self):
        self.admin = UserTable.objects.create_user(
            user_name='Warmup Admin', user_email='warmup-admin@example.com',
            password='StrongPass123!',
        )
        self.admin.is_admin = True
        self.admin.is_active = True
        self.admin.save()

        session = self.client.session
        session['logged_in'] = self.admin.user_email
        session['is_admin'] = True
        session.save()


class WarmupOAuthStartTests(_AdminSessionTestCase):
    def test_start_stores_state_and_pkce_verifier_in_session(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = 'a' * 64  # authorization_url() would have generated this
        mock_flow.authorization_url.return_value = ('https://accounts.google.com/o/oauth2/auth?x=1', 'fake-state-123')

        with patch(f'{WARMUP_MODULE}.oauth_client_config', return_value=FAKE_OAUTH_CONFIG), \
             patch('google_auth_oauthlib.flow.Flow.from_client_config', return_value=mock_flow):
            response = self.client.get(reverse('admin_warmup_receiver_oauth_start'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://accounts.google.com/o/oauth2/auth?x=1')
        session = self.client.session
        self.assertEqual(session.get('warmup_oauth_state'), 'fake-state-123')
        self.assertEqual(session.get('warmup_oauth_code_verifier'), 'a' * 64)

    def test_start_redirects_with_error_when_oauth_not_configured(self):
        with patch(f'{WARMUP_MODULE}.oauth_client_config', return_value=(None, None, None)):
            response = self.client.get(reverse('admin_warmup_receiver_oauth_start'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('error=oauth_not_configured', response.url)


class WarmupOAuthCallbackTests(_AdminSessionTestCase):
    def _seed_session(self, state='fake-state-123', code_verifier='a' * 64):
        session = self.client.session
        if state is not None:
            session['warmup_oauth_state'] = state
        if code_verifier is not None:
            session['warmup_oauth_code_verifier'] = code_verifier
        session.save()

    def test_callback_rejects_mismatched_state(self):
        """Existing state validation must still work, unchanged."""
        self._seed_session(state='fake-state-123')
        response = self.client.get(
            reverse('admin_warmup_receiver_oauth_callback'),
            {'state': 'a-different-state', 'code': 'abc'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('error=oauth_state_mismatch', response.url)

    def test_callback_rejects_missing_state(self):
        # No state ever stored in the session at all.
        response = self.client.get(
            reverse('admin_warmup_receiver_oauth_callback'),
            {'state': 'whatever', 'code': 'abc'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('error=oauth_state_mismatch', response.url)

    def test_callback_with_missing_verifier_returns_controlled_error(self):
        """State matches, but no PKCE verifier is in the session (e.g. an
        expired/cleared session) — must fail with its own distinct error
        code and log clearly, never silently falling through to the
        generic oauth_failed or attempting the exchange at all."""
        self._seed_session(state='fake-state-123', code_verifier=None)

        with patch(f'{WARMUP_MODULE}.oauth_client_config', return_value=FAKE_OAUTH_CONFIG), \
             patch('google_auth_oauthlib.flow.Flow.from_client_config') as mock_from_config:
            response = self.client.get(
                reverse('admin_warmup_receiver_oauth_callback'),
                {'state': 'fake-state-123', 'code': 'abc'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('error=oauth_missing_verifier', response.url)
        # Must fail before ever attempting to build a Flow / exchange a token.
        mock_from_config.assert_not_called()

    def test_callback_passes_stored_verifier_to_flow_and_creates_receiver(self):
        """The full success path: the callback's Flow must be constructed
        with the exact code_verifier stored during the start view, and a
        successful exchange must create/store the receiver with its
        encrypted refresh token — unchanged existing behavior."""
        self._seed_session(state='fake-state-123', code_verifier='the-real-verifier')

        mock_credentials = MagicMock()
        mock_credentials.refresh_token = 'fake-refresh-token'

        mock_flow = MagicMock()
        mock_flow.credentials = mock_credentials
        mock_flow.fetch_token.return_value = None

        with patch(f'{WARMUP_MODULE}.oauth_client_config', return_value=FAKE_OAUTH_CONFIG), \
             patch('google_auth_oauthlib.flow.Flow.from_client_config', return_value=mock_flow) as mock_from_config, \
             patch('googleapiclient.discovery.build', return_value=MagicMock()), \
             patch('Email_validate_app.services.warmup_receiver.get_profile_email',
                   return_value='receiver@example.com'), \
             patch('Email_validate_app.services.warmup_crypto.encrypt_token',
                   return_value='encrypted-token-value') as mock_encrypt:
            response = self.client.get(
                reverse('admin_warmup_receiver_oauth_callback'),
                {'state': 'fake-state-123', 'code': 'abc'},
            )

        # The Flow used for the token exchange must carry the SAME verifier
        # persisted during the start view — this is the actual fix.
        self.assertEqual(mock_from_config.call_args.kwargs.get('code_verifier'), 'the-real-verifier')
        mock_encrypt.assert_called_once_with('fake-refresh-token')

        self.assertEqual(response.status_code, 302)
        self.assertIn('connected=1', response.url)

        receiver = WarmupReceiverAccount.objects.get(email='receiver@example.com')
        self.assertEqual(receiver.status, 'connected')
        self.assertEqual(receiver.refresh_token_encrypted, 'encrypted-token-value')
        self.assertEqual(receiver.user_id, self.admin.id)

        # Both session values must be cleared after a successful connect.
        session = self.client.session
        self.assertIsNone(session.get('warmup_oauth_state'))
        self.assertIsNone(session.get('warmup_oauth_code_verifier'))

    def test_callback_logs_and_redirects_oauth_failed_on_exchange_exception(self):
        """If the exchange still fails for some other reason (e.g. an
        expired code), the existing generic oauth_failed redirect must be
        preserved, now with the failure logged."""
        self._seed_session(state='fake-state-123', code_verifier='the-real-verifier')

        mock_flow = MagicMock()
        mock_flow.fetch_token.side_effect = Exception('invalid_grant: some other failure')

        with patch(f'{WARMUP_MODULE}.oauth_client_config', return_value=FAKE_OAUTH_CONFIG), \
             patch('google_auth_oauthlib.flow.Flow.from_client_config', return_value=mock_flow), \
             patch(f'{WARMUP_MODULE}.logger') as mock_logger:
            response = self.client.get(
                reverse('admin_warmup_receiver_oauth_callback'),
                {'state': 'fake-state-123', 'code': 'abc'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('error=oauth_failed', response.url)
        mock_logger.exception.assert_called_once()
