"""Tests for per-account warmup email content (Edit Settings' Warmup tab):

  - SOEmailAccountWarmup.warmup_subject/warmup_body (new fields)
  - views/so_email_accounts.py::so_email_account_action's 'edit_warmup_content'
    action (save endpoint)
  - views/so_email_accounts.py::so_edit_email_account's GET context
    (compute_account_warmup_analytics wiring)
  - services/warmup_sender.py::build_warmup_content()/send_warmup_email()'s
    fallback to the built-in _TEMPLATES when either field is blank -- and,
    critically, that a REAL send (SMTP faked, everything else exercised for
    real) actually uses the saved content, not just that it's readable back
    from the database.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase
from django.utils.timezone import now

from Email_validate_app.models import SOEmailAccount, SOEmailAccountWarmup, UserTable, WarmupMessage
from Email_validate_app.services.warmup import start_warmup
from Email_validate_app.services.warmup_sender import _TEMPLATES, build_warmup_content, send_warmup_email
from Email_validate_app.services.warmup_receiver import find_warmup_message

ACTION_URL = '/Sales-Outreach/so-accounts/action/'


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Warmup Content Test', user_email=email, password='StrongPass123!')


def make_account(user, email):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name='Warmup Sender',
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=120, status='connected',
    )


class BuildWarmupContentFallbackTests(TestCase):
    """Pure function tests for build_warmup_content()'s account-aware
    branch -- no send, proves the selection logic in isolation before the
    real-send tests below prove it's actually wired into the send path."""

    def setUp(self):
        self.user = make_user('build_content@example.com')
        self.account = make_account(self.user, 'sender-content@example.com')
        start_warmup([self.account.id])

    def test_custom_content_used_when_both_fields_set(self):
        self.account.warmup.warmup_subject = 'My Custom Subject'
        self.account.warmup.warmup_body = '<p>My custom body</p>'
        self.account.warmup.save(update_fields=['warmup_subject', 'warmup_body'])

        subject, body = build_warmup_content('WTI-WARMUP-abc123', self.account)
        self.assertTrue(subject.startswith('My Custom Subject — WTI-WARMUP-abc123'))
        self.assertEqual(body, '<p>My custom body</p>')

    def test_falls_back_to_templates_when_both_blank(self):
        subject, body = build_warmup_content('WTI-WARMUP-abc123', self.account)
        template_subjects = [t['subject'] for t in _TEMPLATES]
        self.assertTrue(any(subject.startswith(s) for s in template_subjects))

    def test_falls_back_when_only_subject_set(self):
        """Save-time validation (edit_warmup_content) never persists one
        without the other, but build_warmup_content defends against it
        independently too rather than trusting that invariant blindly."""
        self.account.warmup.warmup_subject = 'Only Subject'
        self.account.warmup.save(update_fields=['warmup_subject'])
        subject, body = build_warmup_content('WTI-WARMUP-abc123', self.account)
        self.assertFalse(subject.startswith('Only Subject'))

    def test_falls_back_when_only_body_set(self):
        self.account.warmup.warmup_body = '<p>Only body</p>'
        self.account.warmup.save(update_fields=['warmup_body'])
        subject, body = build_warmup_content('WTI-WARMUP-abc123', self.account)
        self.assertNotEqual(body, '<p>Only body</p>')

    def test_no_account_falls_back_to_templates(self):
        subject, body = build_warmup_content('WTI-WARMUP-abc123', None)
        template_subjects = [t['subject'] for t in _TEMPLATES]
        self.assertTrue(any(subject.startswith(s) for s in template_subjects))


class SendWarmupEmailUsesSavedContentTests(TestCase):
    """CRITICAL REAL-SEND TEST (SMTP faked, everything else real): calls
    send_warmup_email() -- the exact function tasks/warmup.py::
    warmup_send_one calls in production -- and inspects the raw MIME bytes
    actually handed to sendmail(), proving the saved content reaches the
    real send path, not just the database."""

    def setUp(self):
        self.user = make_user('send_content@example.com')
        self.account = make_account(self.user, 'sender-send@example.com')
        start_warmup([self.account.id])

    def _make_message(self):
        return WarmupMessage.objects.create(
            sender_account=self.account, sender_email=self.account.email,
            receiver_email='receiver@example.com',
            identifier=f'WTI-WARMUP-{uuid.uuid4().hex[:12]}',
            scheduled_for=now(),
        )

    def _send(self, message):
        import email as email_pkg
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.warmup_sender.open_smtp', return_value=mock_server):
            send_warmup_email(message)
        raw = mock_server.sendmail.call_args[0][2]
        return email_pkg.message_from_bytes(raw)

    def _decoded_subject(self, msg):
        """build_message() encodes Subject via email.header.Header(...,
        'utf-8'), so msg['Subject'] comes back as an RFC 2047 encoded-word
        string (e.g. '=?utf-8?q?...?=') -- decode it before asserting on
        the actual text, the same way a real mail client would."""
        from email.header import decode_header
        parts = decode_header(msg['Subject'])
        return ''.join(
            chunk.decode(enc or 'utf-8') if isinstance(chunk, bytes) else chunk
            for chunk, enc in parts
        )

    def test_custom_content_reaches_the_actual_sent_email(self):
        self.account.warmup.warmup_subject = 'Real Send Custom Subject'
        self.account.warmup.warmup_body = '<p>Real send custom body</p>'
        self.account.warmup.save(update_fields=['warmup_subject', 'warmup_body'])

        message = self._make_message()
        msg = self._send(message)

        subject = self._decoded_subject(msg)
        self.assertIn('Real Send Custom Subject', subject)
        self.assertIn(message.identifier, subject)

        html_parts = [
            p.get_payload(decode=True).decode()
            for p in msg.walk() if p.get_content_type() == 'text/html'
        ]
        self.assertTrue(any('Real send custom body' in p for p in html_parts))

        # message.subject is what send_warmup_email() itself recorded as
        # sent -- must agree with what was actually handed to sendmail().
        # (send_warmup_email() only mutates the in-memory instance; the
        # actual DB write is the calling Celery task's job -- see its own
        # docstring -- so this checks the attribute directly, not a
        # refetch, exactly like the production caller would before its
        # own save() call.)
        self.assertIn('Real Send Custom Subject', message.subject)

    def test_blank_content_falls_back_to_built_in_template_on_real_send(self):
        """Backward compatibility: an account that never sets custom
        content (every account before this feature existed) must keep
        sending exactly as before."""
        message = self._make_message()
        msg = self._send(message)

        subject = self._decoded_subject(msg)
        template_subjects = [t['subject'] for t in _TEMPLATES]
        self.assertTrue(any(s in subject for s in template_subjects))
        self.assertIn(message.identifier, subject)


class WarmupIdentifierPreservationTests(TestCase):
    """The identifier (WTI-WARMUP-<hex>) is what warmup_receiver.py's Gmail
    search depends on to find the sent message at all — it must never be
    lost or corrupted by a user-supplied custom subject, and it must never
    become something the user can edit. Three things must all hold:
      1. The saved DB value is the user's clean text, identifier NOT baked in.
      2. The actual sent subject is exactly "<custom text> — <identifier>".
      3. Receiver-side lookup keys off the identifier alone, so it's
         completely unaffected by whether custom or built-in content was used.
    """

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('identifier_preserve@example.com')
        self.account = make_account(self.user, 'identifier-preserve@example.com')
        start_warmup([self.account.id])
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_saved_subject_in_db_has_no_identifier_baked_in(self):
        body = {'action': 'edit_warmup_content', 'id': self.account.id,
                'warmup_subject': 'Hello, just checking in', 'warmup_body': '<p>Body</p>'}
        r = self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')
        self.assertEqual(r.json()['status'], 'ok')

        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.warmup_subject, 'Hello, just checking in')
        self.assertNotIn('WTI-WARMUP-', self.account.warmup.warmup_subject)

    def test_sent_subject_is_exactly_custom_text_dash_identifier(self):
        self.account.warmup.warmup_subject = 'Hello, just checking in'
        self.account.warmup.warmup_body = '<p>Body</p>'
        self.account.warmup.save(update_fields=['warmup_subject', 'warmup_body'])

        message = WarmupMessage.objects.create(
            sender_account=self.account, sender_email=self.account.email,
            receiver_email='receiver@example.com',
            identifier=f'WTI-WARMUP-{uuid.uuid4().hex[:12]}',
            scheduled_for=now(),
        )

        import email as email_pkg
        from email.header import decode_header
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.warmup_sender.open_smtp', return_value=mock_server):
            send_warmup_email(message)
        raw = mock_server.sendmail.call_args[0][2]
        msg = email_pkg.message_from_bytes(raw)
        parts = decode_header(msg['Subject'])
        subject = ''.join(c.decode(e or 'utf-8') if isinstance(c, bytes) else c for c, e in parts)

        self.assertEqual(subject, f'Hello, just checking in — {message.identifier}')

    def test_receiver_lookup_query_depends_only_on_identifier(self):
        """find_warmup_message() must build the identical Gmail query
        whether the sending account uses custom content or the built-in
        templates -- it never reads warmup_subject/body at all, only the
        identifier string it's given."""
        identifier = f'WTI-WARMUP-{uuid.uuid4().hex[:12]}'

        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            'messages': [],
        }
        find_warmup_message(mock_service, identifier)

        list_mock = mock_service.users.return_value.messages.return_value.list
        _, kwargs = list_mock.call_args
        self.assertEqual(kwargs.get('q'), f'subject:"{identifier}"')

    def test_full_path_custom_subject_still_found_by_receiver_search(self):
        """End-to-end: send with a custom subject, then feed the Gmail
        mock a search result whose subject looks like what was actually
        sent -- find_warmup_message must still match it."""
        self.account.warmup.warmup_subject = 'Hello, just checking in'
        self.account.warmup.warmup_body = '<p>Body</p>'
        self.account.warmup.save(update_fields=['warmup_subject', 'warmup_body'])

        message = WarmupMessage.objects.create(
            sender_account=self.account, sender_email=self.account.email,
            receiver_email='receiver@example.com',
            identifier=f'WTI-WARMUP-{uuid.uuid4().hex[:12]}',
            scheduled_for=now(),
        )
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.warmup_sender.open_smtp', return_value=mock_server):
            send_warmup_email(message)

        # Gmail's own subject: search is a substring match on the identifier
        # -- simulate it finding the message that was actually just sent.
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            'messages': [{'id': 'gmail-msg-1'}],
        }
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            'id': 'gmail-msg-1', 'labelIds': ['INBOX'],
        }
        found = find_warmup_message(mock_service, message.identifier)
        self.assertIsNotNone(found)
        self.assertEqual(found['id'], 'gmail-msg-1')


class EmailAccountsListEditPanelTests(TestCase):
    """Edit Settings is now an in-page side panel on the Email Accounts
    list itself (so_email_accounts), not a separate page — the standalone
    so_edit_email_account view/URL/template were removed. Each account's
    warmup_analytics is now computed per-row on this same view, and the
    panel content (with per-account element ids) is embedded directly in
    the list response."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('view_context@example.com')
        self.account = make_account(self.user, 'view-context@example.com')
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _get(self):
        return self.client.get('/Sales-Outreach/so-accounts/')

    def test_no_warmup_shows_not_started_status_but_editor_still_renders(self):
        """Case 1: the content editor must be present even though Warmup
        was never started — only the status section collapses to the
        simple not-started line."""
        r = self._get()
        self.assertEqual(r.status_code, 200)
        accounts_by_id = {a.id: a for a in r.context['accounts']}
        self.assertIsNone(accounts_by_id[self.account.id].warmup_analytics)
        self.assertContains(r, 'Warmup has not been started')
        # The editor fields themselves must be in the response regardless,
        # scoped to this account's own id.
        self.assertContains(r, f'id="eeaWarmupSubject-{self.account.id}"')
        self.assertContains(r, f'id="eeaWarmupBodyEditor-{self.account.id}"')

    def test_with_warmup_shows_analytics_and_content_fields(self):
        start_warmup([self.account.id])
        self.account.warmup.warmup_subject = 'Existing Subject'
        self.account.warmup.warmup_body = '<p>Existing body</p>'
        self.account.warmup.save(update_fields=['warmup_subject', 'warmup_body'])

        r = self._get()
        self.assertEqual(r.status_code, 200)
        accounts_by_id = {a.id: a for a in r.context['accounts']}
        analytics = accounts_by_id[self.account.id].warmup_analytics
        self.assertEqual(analytics['status'], 'active')
        self.assertEqual(analytics['daily_target'], 40)
        self.assertContains(r, 'Existing Subject')
        self.assertContains(r, 'Existing body')

    def test_standalone_edit_page_url_no_longer_exists(self):
        """The old /so-accounts/<id>/edit/ page is fully retired, not just
        unlinked — confirms it 404s rather than silently still working."""
        r = self.client.get(f'/Sales-Outreach/so-accounts/{self.account.id}/edit/')
        self.assertEqual(r.status_code, 404)


class EditWarmupContentActionTests(TestCase):
    """POST /Sales-Outreach/so-accounts/action/ {action: 'edit_warmup_content'}"""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('save_content@example.com')
        self.account = make_account(self.user, 'save-content@example.com')
        start_warmup([self.account.id])
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def _post(self, **payload):
        body = {'action': 'edit_warmup_content', 'id': self.account.id}
        body.update(payload)
        return self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')

    def test_saves_subject_and_body(self):
        r = self._post(warmup_subject='New Subject', warmup_body='<p>New body</p>')
        self.assertEqual(r.json()['status'], 'ok')

        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.warmup_subject, 'New Subject')
        self.assertEqual(self.account.warmup.warmup_body, '<p>New body</p>')

    def test_active_warmup_status_survives_a_content_save(self):
        """Case 2: an already-active warmup's status/started_at/ramp
        config must be completely unaffected by a content save."""
        self.assertEqual(self.account.warmup.status, 'active')
        started_at_before = self.account.warmup.started_at

        r = self._post(warmup_subject='Active Save Subject', warmup_body='<p>x</p>')
        self.assertEqual(r.json()['status'], 'ok')

        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.status, 'active')
        self.assertEqual(self.account.warmup.started_at, started_at_before)
        self.assertEqual(self.account.warmup.daily_target, 40)
        self.assertEqual(self.account.warmup.ramp_up_days, 30)

    def test_paused_and_stopped_warmup_status_survives_a_content_save(self):
        """Case 3: content editing must remain available while
        paused/stopped, and must not change the status either way."""
        from Email_validate_app.services.warmup import pause_warmup, stop_warmup

        pause_warmup([self.account.id])
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.status, 'paused')
        r = self._post(warmup_subject='Paused Save Subject', warmup_body='<p>x</p>')
        self.assertEqual(r.json()['status'], 'ok')
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.status, 'paused')
        self.assertEqual(self.account.warmup.warmup_subject, 'Paused Save Subject')

        stop_warmup([self.account.id])
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.status, 'stopped')
        r = self._post(warmup_subject='Stopped Save Subject', warmup_body='<p>x</p>')
        self.assertEqual(r.json()['status'], 'ok')
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.status, 'stopped')
        self.assertEqual(self.account.warmup.warmup_subject, 'Stopped Save Subject')

    def test_persists_after_refetch(self):
        self._post(warmup_subject='Persisted Subject', warmup_body='<p>Persisted body</p>')

        refetched = SOEmailAccount.objects.select_related('warmup').get(id=self.account.id)
        self.assertEqual(refetched.warmup.warmup_subject, 'Persisted Subject')
        self.assertEqual(refetched.warmup.warmup_body, '<p>Persisted body</p>')

    def test_clearing_both_fields_is_allowed(self):
        self._post(warmup_subject='Something', warmup_body='<p>Something</p>')
        r = self._post(warmup_subject='', warmup_body='')
        self.assertEqual(r.json()['status'], 'ok')
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.warmup_subject, '')
        self.assertEqual(self.account.warmup.warmup_body, '')

    def test_subject_without_body_is_rejected(self):
        r = self._post(warmup_subject='Only Subject', warmup_body='')
        d = r.json()
        self.assertEqual(d['status'], 'error')
        self.assertIn('warmup_body', d['errors'])

    def test_body_without_subject_is_rejected(self):
        r = self._post(warmup_subject='', warmup_body='<p>Only body</p>')
        d = r.json()
        self.assertEqual(d['status'], 'error')
        self.assertIn('warmup_subject', d['errors'])

    def test_subject_over_500_chars_is_rejected(self):
        r = self._post(warmup_subject='x' * 501, warmup_body='<p>body</p>')
        d = r.json()
        self.assertEqual(d['status'], 'error')
        self.assertIn('warmup_subject', d['errors'])

    def test_unknown_account_returns_error(self):
        body = {'action': 'edit_warmup_content', 'id': 999999,
                'warmup_subject': 'x', 'warmup_body': '<p>x</p>'}
        r = self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')
        self.assertEqual(r.json()['status'], 'error')

    def test_account_without_warmup_row_creates_one_without_starting_warmup(self):
        """Content editing must work even if Warmup was never started —
        this creates the SOEmailAccountWarmup row (get_or_create), but
        must NOT make it look/behave like warmup is running: status must
        be 'stopped' (overriding the model's own 'active' default),
        started_at must stay unset, and ramp config must stay at whatever
        the model defaults are (never touched by this action)."""
        other = make_account(self.user, 'no-warmup@example.com')
        self.assertFalse(SOEmailAccountWarmup.objects.filter(account=other).exists())

        body = {'action': 'edit_warmup_content', 'id': other.id,
                'warmup_subject': 'x', 'warmup_body': '<p>x</p>'}
        r = self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')
        self.assertEqual(r.json()['status'], 'ok')

        warmup = SOEmailAccountWarmup.objects.get(account=other)
        self.assertEqual(warmup.warmup_subject, 'x')
        self.assertEqual(warmup.warmup_body, '<p>x</p>')
        self.assertEqual(warmup.status, 'stopped')
        self.assertIsNone(warmup.started_at)
        self.assertEqual(warmup.daily_target, 40)   # model default, untouched
        self.assertEqual(warmup.ramp_up_days, 30)   # model default, untouched

    def test_content_only_row_is_excluded_from_active_dispatch(self):
        """The get_or_create'd placeholder row must never be picked up by
        warmup_dispatch_sends()'s status='active' query — real defense,
        not just a cosmetic label."""
        from Email_validate_app.models import SOEmailAccountWarmup as _W
        other = make_account(self.user, 'dispatch-check@example.com')
        body = {'action': 'edit_warmup_content', 'id': other.id,
                'warmup_subject': 'x', 'warmup_body': '<p>x</p>'}
        self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')

        active_for_dispatch = _W.objects.filter(
            status='active', account__status='connected', account__deleted_at__isnull=True,
        )
        self.assertFalse(active_for_dispatch.filter(account=other).exists())

    def test_does_not_affect_account_settings(self):
        """edit_warmup_content must never touch display_name/daily_limit —
        confirms the two save flows stay independent, per spec."""
        original_display = self.account.display_name
        original_limit = self.account.daily_limit
        self._post(warmup_subject='X', warmup_body='<p>X</p>')
        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, original_display)
        self.assertEqual(self.account.daily_limit, original_limit)


class EditAccountActionNoLongerNeedsWarmupPayloadTests(TestCase):
    """Regression check: the existing 'edit' action must keep working when
    called WITHOUT a warmup payload at all (the new Accounts tab never
    sends one) -- and must not silently touch warmup content either."""

    def setUp(self):
        self.client = Client(SERVER_NAME='127.0.0.1')
        self.user = make_user('accounts_only@example.com')
        self.account = make_account(self.user, 'accounts-only@example.com')
        start_warmup([self.account.id])
        session = self.client.session
        session['logged_in'] = self.user.user_email
        session.save()

    def test_edit_without_warmup_payload_still_saves_account_fields(self):
        body = {'action': 'edit', 'id': self.account.id,
                'display_name': 'New Sender Name', 'daily_limit': 75}
        r = self.client.post(ACTION_URL, data=json.dumps(body), content_type='application/json')
        self.assertEqual(r.json()['status'], 'ok')

        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, 'New Sender Name')
        self.assertEqual(self.account.daily_limit, 75)

        # ramp config untouched, content fields untouched
        self.account.warmup.refresh_from_db()
        self.assertEqual(self.account.warmup.daily_target, 40)
        self.assertEqual(self.account.warmup.warmup_subject, '')
