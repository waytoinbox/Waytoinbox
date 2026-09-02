"""Phase 1 tests for the Warmup audit's Gmail spam-detection fix.

Covers:
  A. find_warmup_message() searches with includeSpamTrash=True.
  B. classify_landing() correctly identifies a SPAM-labeled message.
  C. classify_landing() correctly identifies an INBOX-labeled message.
  D. rescue_from_spam() issues the correct removeLabelIds/addLabelIds
     modify() call, and — at the task level (warmup_check_one) — the
     original 'spam' classification survives a successful rescue
     alongside rescued_to_inbox=True.
  F. An empty receiver pool: no WarmupMessage rows are created, a warning
     is logged, and the dispatcher does not raise.

No real Gmail API calls are made anywhere in this file — the `service`
object is always a MagicMock.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils.timezone import now

from Email_validate_app.models import (
    UserTable, SOEmailAccount, SOEmailAccountWarmup, WarmupReceiverAccount,
    WarmupMessage,
)
from Email_validate_app.services.warmup_receiver import (
    find_warmup_message, classify_landing, rescue_from_spam,
)
from Email_validate_app.services.warmup import create_pending_messages_for_sender


def make_user(email):
    return UserTable.objects.create_user(
        user_name='Warmup Test', user_email=email, password='StrongPass123!')


# ── A. Gmail search includes spam/trash ─────────────────────────────────

class FindWarmupMessageTests(TestCase):
    def test_search_passes_includeSpamTrash_true(self):
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            'messages': [{'id': 'msg-1'}],
        }
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            'id': 'msg-1', 'labelIds': ['INBOX'],
        }

        find_warmup_message(mock_service, 'WTI-WARMUP-abc123')

        list_mock = mock_service.users.return_value.messages.return_value.list
        list_mock.assert_called_once()
        _, kwargs = list_mock.call_args
        self.assertIs(kwargs.get('includeSpamTrash'), True)
        self.assertEqual(kwargs.get('q'), 'subject:"WTI-WARMUP-abc123"')

    def test_no_match_returns_none(self):
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            'messages': [],
        }
        result = find_warmup_message(mock_service, 'WTI-WARMUP-nomatch')
        self.assertIsNone(result)


# ── B/C. Label-based classification (unchanged logic, verified) ────────

class ClassifyLandingTests(TestCase):
    def test_spam_label_classifies_as_spam(self):
        self.assertEqual(classify_landing(['SPAM', 'UNREAD']), 'spam')

    def test_inbox_label_classifies_as_inbox(self):
        self.assertEqual(classify_landing(['INBOX', 'UNREAD']), 'inbox')

    def test_neither_label_classifies_as_other(self):
        self.assertEqual(classify_landing(['CATEGORY_PROMOTIONS']), 'other')

    def test_spam_takes_priority_if_both_present(self):
        # Gmail never actually emits both, but the priority order itself
        # (SPAM checked first) is part of the contract being preserved.
        self.assertEqual(classify_landing(['SPAM', 'INBOX']), 'spam')


# ── D. Spam rescue — service-level call shape ───────────────────────────

class RescueFromSpamTests(TestCase):
    def test_rescue_removes_spam_and_unread_adds_inbox(self):
        mock_service = MagicMock()
        rescue_from_spam(mock_service, 'msg-1', was_unread=True)

        modify_mock = mock_service.users.return_value.messages.return_value.modify
        modify_mock.assert_called_once_with(
            userId='me', id='msg-1',
            body={'removeLabelIds': ['SPAM', 'UNREAD'], 'addLabelIds': ['INBOX']},
        )

    def test_rescue_when_already_read_does_not_touch_unread(self):
        mock_service = MagicMock()
        rescue_from_spam(mock_service, 'msg-2', was_unread=False)

        modify_mock = mock_service.users.return_value.messages.return_value.modify
        modify_mock.assert_called_once_with(
            userId='me', id='msg-2',
            body={'removeLabelIds': ['SPAM'], 'addLabelIds': ['INBOX']},
        )


# ── D (task level) + B/C (task level) — the full warmup_check_one flow ──

class WarmupCheckOneTests(TestCase):
    def setUp(self):
        self.user = make_user('warmup_check_one@example.com')
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Warmup Sender',
            email='warmup-sender-test@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='warmup-sender-test@example.com',
            password='x', daily_limit=50, status='connected',
        )
        self.receiver = WarmupReceiverAccount.objects.create(
            user_id=self.user.id, email='warmup-receiver-test@example.com',
            refresh_token_encrypted='irrelevant-for-this-test', status='connected',
        )
        self.message = WarmupMessage.objects.create(
            sender_account=self.account, sender_email=self.account.email,
            receiver_account=self.receiver, receiver_email=self.receiver.email,
            identifier='WTI-WARMUP-testcheck', status='sent',
            scheduled_for=now(), sent_at=now(), check_after=now(),
        )

    def _run_check(self, found_labels):
        from Email_validate_app.tasks.warmup import warmup_check_one

        mock_service = MagicMock()
        with patch('Email_validate_app.services.warmup_receiver.get_gmail_service', return_value=mock_service), \
             patch('Email_validate_app.services.warmup_receiver.find_warmup_message',
                   return_value={'id': 'gmail-msg-1', 'labelIds': found_labels}):
            warmup_check_one(self.message.id)
        self.message.refresh_from_db()
        return mock_service

    def test_inbox_message_marks_read_and_records_inbox(self):
        mock_service = self._run_check(['INBOX', 'UNREAD'])

        self.assertEqual(self.message.landing_location, 'inbox')
        self.assertFalse(self.message.rescued_to_inbox)
        self.assertTrue(self.message.marked_read)
        self.assertEqual(self.message.status, 'completed')

        modify_mock = mock_service.users.return_value.messages.return_value.modify
        modify_mock.assert_called_once_with(
            userId='me', id='gmail-msg-1', body={'removeLabelIds': ['UNREAD']},
        )

    def test_spam_message_is_rescued_and_original_classification_survives(self):
        """The key requirement from the audit: landing_location must stay
        'spam' (the ORIGINAL classification) even though the message was
        successfully moved to Inbox — rescued_to_inbox is the separate
        fact that records the move itself."""
        mock_service = self._run_check(['SPAM', 'UNREAD'])

        self.assertEqual(self.message.landing_location, 'spam')
        self.assertTrue(self.message.rescued_to_inbox)
        self.assertTrue(self.message.marked_read)
        self.assertEqual(self.message.status, 'completed')

        modify_mock = mock_service.users.return_value.messages.return_value.modify
        modify_mock.assert_called_once_with(
            userId='me', id='gmail-msg-1',
            body={'removeLabelIds': ['SPAM', 'UNREAD'], 'addLabelIds': ['INBOX']},
        )

    def test_other_landing_is_left_untouched(self):
        mock_service = self._run_check(['CATEGORY_PROMOTIONS'])

        self.assertEqual(self.message.landing_location, 'other')
        self.assertFalse(self.message.rescued_to_inbox)
        modify_mock = mock_service.users.return_value.messages.return_value.modify
        modify_mock.assert_not_called()


# ── F. Empty receiver pool — dispatcher no-ops safely, logs a warning ───

class EmptyReceiverPoolTests(TestCase):
    def setUp(self):
        self.user = make_user('warmup_empty_pool@example.com')

    def test_no_receivers_creates_nothing_and_logs_warning(self):
        account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Warmup Sender 2',
            email='warmup-sender-test2@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='warmup-sender-test2@example.com',
            password='x', daily_limit=50, status='connected',
        )
        warmup = SOEmailAccountWarmup.objects.create(
            account=account, daily_target=40, ramp_up_days=30, ramp_up_increment=2,
            started_at=now() - timedelta(days=1), status='active',
        )
        self.assertEqual(WarmupReceiverAccount.objects.filter(status='connected').count(), 0)

        with patch('Email_validate_app.services.warmup.logger') as mock_logger:
            created = create_pending_messages_for_sender(warmup)

        self.assertEqual(created, 0)
        self.assertEqual(WarmupMessage.objects.filter(sender_account=account).count(), 0)
        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args[0]
        self.assertIn('no connected', warning_args[0])

    def test_dispatcher_does_not_raise_and_next_tick_still_works(self):
        """Simulates two consecutive Beat ticks with an empty pool, then a
        receiver appearing before the third tick -- the dispatcher must
        keep working normally once one connects, not get stuck."""
        account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Warmup Sender 3',
            email='warmup-sender-test3@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='warmup-sender-test3@example.com',
            password='x', daily_limit=50, status='connected',
        )
        warmup = SOEmailAccountWarmup.objects.create(
            account=account, daily_target=40, ramp_up_days=30, ramp_up_increment=2,
            started_at=now() - timedelta(days=1), status='active',
        )

        # Tick 1 & 2: empty pool, no exception, no rows.
        for _ in range(2):
            created = create_pending_messages_for_sender(warmup)
            self.assertEqual(created, 0)

        # A receiver connects.
        WarmupReceiverAccount.objects.create(
            user_id=self.user.id, email='warmup-receiver-test3@example.com',
            refresh_token_encrypted='x', status='connected',
        )

        # Tick 3: now works normally.
        created = create_pending_messages_for_sender(warmup)
        self.assertGreater(created, 0)
        self.assertTrue(WarmupMessage.objects.filter(sender_account=account).exists())
