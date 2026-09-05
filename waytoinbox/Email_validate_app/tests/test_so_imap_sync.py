"""Regression tests for the Sales Outreach Bounce & Reply investigation fix.

Covers services/so_imap.py's message-classification cascade
(sync_account_inbox) end to end against a fake IMAP server — no real
network/IMAP connection is ever made. Also covers the services/so_drip.py
SMTP-time-rejection classification and outbound threading changes that
ship alongside it.

Root causes fixed (see the investigation report):
  1. A DSN/bounce notification carrying In-Reply-To/References was
     classified as a Reply before bounce detection ever ran (branch
     order). Fixed: bounce is now tested FIRST.
  2. Reply matching fell back to a bare From-address match with no
     verification that the message actually arrived at the account that
     sent the campaign, and with no exclusion for the user's own connected
     sender accounts -- letting a campaign's own delivery copy (landing in
     a connected-account recipient's synced inbox) look like a reply.
  3. An SMTP-time hard rejection (smtplib.SMTPRecipientsRefused, 5xx) was
     folded into the generic failure/retry path instead of being recorded
     as a bounce.
  4. An out-of-office/auto-reply's 'replied' event was written before the
     OOF check ran, inflating total_replied even though the sequence
     itself was correctly left un-stopped.

Follows this project's established fixture/mocking conventions (see
test_so_drip_send.py's SendNextStepTests) -- Django's isolated test
database only, IMAP faked at the imaplib.IMAP4_SSL boundary, SMTP faked at
services.so_smtp.open_smtp exactly like the existing drip tests.
"""
import uuid
from datetime import time
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from Email_validate_app.models import (
    UserTable, SOCampaign, SOCampaignContact, SOProspect, SOEmailAccount,
    SOEvent, SOSequenceStep, SOSequenceVariant, SOConversation, SOMessage,
)
from Email_validate_app.services import so_imap, so_drip


def make_user(email):
    return UserTable.objects.create_user(
        user_name='IMAP Sync Test', user_email=email, password='StrongPass123!')


def make_account(user, email, sent_folder='Sent'):
    return SOEmailAccount.objects.create(
        user_id=user.id, provider='google', display_name=email.split('@')[0],
        email=email, smtp_host='smtp.test', smtp_port=587,
        imap_host='imap.test', imap_port=993, username=email,
        password='x', daily_limit=50, status='connected',
        # Pre-set so _discover_sent_folder never needs a real IMAP LIST.
        sent_folder=sent_folder,
    )


def make_campaign(user, name='Campaign'):
    campaign = SOCampaign.objects.create(
        user_id=user.id, name=name, subject='s', html_body='<p>x</p>',
        status='sending', tracking_enabled=True,
    )
    step = SOSequenceStep.objects.create(campaign=campaign, order=1, wait_days=0, wait_hours=0)
    SOSequenceVariant.objects.create(
        step=step, label='A', subject='Hello', html_body='<p>x</p>', weight=100, is_active=True,
    )
    return campaign


def make_sent_contact(campaign, account, email, message_id, sent_at=None):
    """A SOCampaignContact + matching SOProspect as if a real send already
    completed for it -- sent_at/message_id set exactly like
    so_drip.py::_record_success would leave them."""
    from django.utils.timezone import now
    prospect = SOProspect.objects.filter(user_id=campaign.user_id, email__iexact=email).first()
    if not prospect:
        prospect = SOProspect.objects.create(
            user_id=campaign.user_id, email=email, first_name='T', last_name='P', status='subscribed',
        )
    cc = SOCampaignContact.objects.create(
        campaign=campaign, prospect=prospect, email=email, account=account,
        status='active', current_step=1, message_id=message_id, sent_at=sent_at or now(),
    )
    SOEvent.objects.create(
        campaign=campaign, prospect=prospect, account=account, message_id=message_id,
        email=email, event_type='sent', metadata={'step': 1}, step_order=1,
    )
    SOEvent.objects.create(
        campaign=campaign, prospect=prospect, account=account, message_id=message_id,
        email=email, event_type='delivered', metadata={'step': 1}, step_order=1,
    )
    return cc


def _msgid():
    return f'<{uuid.uuid4()}@relay.test>'


def _raw_message(headers: dict, body: str = 'Hello there.') -> bytes:
    lines = [f'{k}: {v}' for k, v in headers.items()]
    lines.append('')
    lines.append(body)
    return ('\r\n'.join(lines)).encode('utf-8')


def _raw_dsn(from_addr, to_addr, status, in_reply_to=None, failed_recipient=None,
            include_multipart=True):
    """A realistic RFC 3464 delivery-status notification, matching what
    Gmail actually sends: multipart/report; report-type=delivery-status,
    a message/delivery-status sub-part carrying Status:, plus
    X-Failed-Recipients (Gmail's own convention) and In-Reply-To/References
    quoting the original send -- both signals real bounces carry."""
    boundary = 'BOUNDARY-' + uuid.uuid4().hex
    headers = {
        'From': f'Mail Delivery Subsystem <mailer-daemon@googlemail.com>',
        'To': to_addr,
        'Subject': 'Delivery Status Notification (Failure)',
        'Message-ID': _msgid(),
    }
    if failed_recipient:
        headers['X-Failed-Recipients'] = failed_recipient
    if in_reply_to:
        headers['In-Reply-To'] = in_reply_to
        headers['References'] = in_reply_to
    if include_multipart:
        headers['Content-Type'] = f'multipart/report; report-type=delivery-status; boundary="{boundary}"'
        body = '\r\n'.join([
            f'--{boundary}',
            'Content-Type: text/plain',
            '',
            'Your message could not be delivered.',
            f'--{boundary}',
            'Content-Type: message/delivery-status',
            '',
            'Reporting-MTA: dns; mx.example.com',
            'Action: failed',
            f'Status: {status}',
            (f'Final-Recipient: rfc822; {failed_recipient}' if failed_recipient else ''),
            f'--{boundary}--',
        ])
    else:
        body = 'Your message could not be delivered.'
    lines = [f'{k}: {v}' for k, v in headers.items()] + ['', body]
    return ('\r\n'.join(lines)).encode('utf-8')


class _FakeIMAP:
    """Minimal imaplib-shaped fake. `inbox` is {b'1': raw_bytes, ...} for
    INBOX; `sent` (default empty) for the discovered Sent folder. The real
    IMAP SINCE-date filter is not applied -- tests control exactly what
    "arrives" via the dict contents. Every fetch (HEADER-only or full
    RFC822) returns the full raw bytes regardless of the requested parts --
    email.message_from_bytes parses whatever it's given identically either
    way, so this is a safe simplification, not a behavior gap."""

    def __init__(self, inbox=None, sent=None):
        self.inbox = inbox or {}
        self.sent = sent or {}
        self._store = self.inbox

    def login(self, username, password):
        return 'OK', [b'Logged in']

    def select(self, mailbox, readonly=True):
        self._store = self.inbox if mailbox == 'INBOX' else self.sent
        return 'OK', [str(len(self._store)).encode()]

    def search(self, charset, criterion):
        nums = b' '.join(sorted(self._store.keys()))
        return 'OK', [nums]

    def fetch(self, num, parts):
        raw = self._store.get(num, b'')
        return 'OK', [(b'%s (FETCH {%d}' % (num, len(raw)), raw), b')']

    def list(self):
        return 'OK', [b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def logout(self):
        return 'BYE', [b'Logging out']


def _sync(account, inbox_messages):
    """Run sync_account_inbox against a fake IMAP server pre-loaded with
    `inbox_messages` ({b'1': raw_bytes, ...}), with no real network/IMAP
    connection and no real password decryption."""
    fake = _FakeIMAP(inbox=inbox_messages)
    with patch('Email_validate_app.services.so_imap.imaplib.IMAP4_SSL', return_value=fake), \
         patch('Email_validate_app.services.so_smtp.decrypt_password', return_value='x'):
        so_imap.sync_account_inbox(account)


class BounceBeforeReplyOrderingTests(TestCase):
    """Fix 1/2/4: bounce classification must run before, and pre-empt,
    reply detection -- even though a well-formed DSN carries exactly the
    In-Reply-To/References headers reply detection looks for."""

    def setUp(self):
        self.user = make_user('bounce_order@example.com')
        self.account = make_account(self.user, 'sender@example.com')
        self.campaign = make_campaign(self.user)

    def test_dsn_with_in_reply_to_becomes_bounced_not_replied(self):
        """Test 4 / Test 11 -- the exact bug this investigation reproduced."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'pra68sant@amgil.com', msg_id)

        dsn = _raw_dsn('mailer-daemon@googlemail.com', self.account.email,
                       status='5.1.1', in_reply_to=msg_id, failed_recipient=cc.email)
        _sync(self.account, {b'1': dsn})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 1)
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)
        # sent+delivered from make_sent_contact are untouched by the bounce.
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='sent').count(), 1)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 1)
        self.assertEqual(self.campaign.total_replied, 0)

    def test_bounce_stops_the_contact_and_cannot_be_reverted_by_a_later_send_attempt(self):
        """Test 12 -- bounced must remain the final state."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'dead@amgil.com', msg_id)

        dsn = _raw_dsn('mailer-daemon@googlemail.com', self.account.email,
                       status='5.1.1', in_reply_to=msg_id, failed_recipient=cc.email)
        _sync(self.account, {b'1': dsn})

        cc.refresh_from_db()
        self.assertEqual(cc.status, 'stopped')

        # A later send attempt on this now-bounced contact must not create a
        # fresh 'sent' event or revive it -- send_next_step's own
        # pre-send suppression check (services/so_drip.py) is what
        # enforces this; confirmed here as a regression guard.
        SOCampaignContact.objects.filter(id=cc.id).update(status='sending')
        cc.refresh_from_db()
        result = so_drip.send_next_step(cc)

        self.assertFalse(result)
        cc.refresh_from_db()
        self.assertEqual(cc.status, 'stopped')
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, email=cc.email, event_type='sent').count(), 1)

    def test_bounce_without_x_failed_recipients_matches_via_in_reply_to_fallback(self):
        """The bounce branch's own In-Reply-To fallback (used when a DSN
        doesn't name the address in X-Failed-Recipients) still works after
        the reordering."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'nofailedhdr@amgil.com', msg_id)

        dsn = _raw_dsn('mailer-daemon@googlemail.com', self.account.email,
                       status='5.1.1', in_reply_to=msg_id, failed_recipient=None)
        _sync(self.account, {b'1': dsn})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 1)


class SoftBounceTests(TestCase):
    """Test 5 -- a real, parseable 4.x.x DSN must not be treated as a
    permanent bounce."""

    def setUp(self):
        self.user = make_user('soft_bounce@example.com')
        self.account = make_account(self.user, 'sender2@example.com')
        self.campaign = make_campaign(self.user)

    def test_soft_bounce_is_not_recorded_or_suppressed(self):
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'mailboxfull@example.com', msg_id)

        dsn = _raw_dsn('mailer-daemon@googlemail.com', self.account.email,
                       status='4.2.2', in_reply_to=msg_id, failed_recipient=cc.email)
        _sync(self.account, {b'1': dsn})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 0)
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 0)
        cc.refresh_from_db()
        self.assertNotEqual(cc.status, 'stopped')

    def test_unparseable_bounce_still_defaults_to_hard_for_backward_compatibility(self):
        """A bounce-looking message that isn't a well-formed DSN body (the
        old subject/From heuristic only) keeps today's exact behavior --
        treated as a hard bounce, not silently dropped."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'legacybounce@example.com', msg_id)

        legacy_bounce = _raw_message({
            'From': 'MAILER-DAEMON@example.com',
            'To': self.account.email,
            'Subject': 'Undelivered Mail Returned to Sender',
            'Message-ID': _msgid(),
            'X-Failed-Recipients': cc.email,
        }, body='Delivery failed.')
        _sync(self.account, {b'1': legacy_bounce})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 1)


class SMTPTimeRejectionTests(TestCase):
    """Test 3 -- a hard rejection caught synchronously during the SMTP
    transaction must be classified as a bounce, not a generic failure."""

    def setUp(self):
        self.user = make_user('smtp_reject@example.com')
        self.account = SOEmailAccount.objects.create(
            user_id=self.user.id, provider='google', display_name='Sender',
            email='smtpsender@example.com', smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username='smtpsender@example.com',
            password='x', daily_limit=50, status='connected',
        )
        self.campaign = make_campaign(self.user)
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='pra68sant@amgil.com', first_name='T', last_name='P',
            status='subscribed',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )

    def _send_with_refusal(self, code, message=b'user unknown'):
        import smtplib
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
            {self.cc.email: (code, message)}
        )
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            return so_drip.send_next_step(self.cc)

    def test_hard_smtp_rejection_records_bounced_not_failed(self):
        result = self._send_with_refusal(550, b'5.1.1 user unknown')

        self.assertFalse(result)
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 1)
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='failed').count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 1)
        self.assertEqual(self.campaign.total_failed, 0)
        self.cc.refresh_from_db()
        self.assertEqual(self.cc.status, 'stopped')

    def test_soft_smtp_rejection_keeps_existing_retry_behavior(self):
        result = self._send_with_refusal(450, b'4.2.1 mailbox temporarily unavailable')

        self.assertFalse(result)
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='bounced').count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_bounced, 0)
        self.cc.refresh_from_db()
        # Existing _record_failure retry path: still active, attempts incremented.
        self.assertEqual(self.cc.status, 'active')
        self.assertEqual(self.cc.attempts, 1)


class ReplyMatchingTests(TestCase):
    """Tests 6, 7, 9, 10 -- genuine replies match; unrelated mail and
    campaign-copy-in-a-synced-inbox mail don't."""

    def setUp(self):
        self.user = make_user('reply_match@example.com')
        self.account = make_account(self.user, 'outreach@example.com')
        self.campaign = make_campaign(self.user)

    def test_real_reply_with_in_reply_to_is_recorded(self):
        """Test 6."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'external@example.com', msg_id)

        reply = _raw_message({
            'From': cc.email, 'To': self.account.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Sounds interesting, tell me more.')
        _sync(self.account, {b'1': reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 1)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_replied, 1)

    def test_unrelated_inbound_email_is_not_a_reply(self):
        """Test 10."""
        unrelated = _raw_message({
            'From': 'random-person@somewhere.com', 'To': self.account.email,
            'Subject': 'Hello there', 'Message-ID': _msgid(),
        }, body='Just saying hi, unrelated to any campaign.')
        _sync(self.account, {b'1': unrelated})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)
        # It still gets recorded as ordinary mail (Others), just not a reply.
        self.assertTrue(SOMessage.objects.filter(from_email='random-person@somewhere.com').exists())


class SenderRecipientCollisionTests(TestCase):
    """Fix 5/6, Tests 7, 8, 9 -- the exact reported scenario: two connected
    accounts of the same user, one used as the other's campaign recipient."""

    def setUp(self):
        self.user = make_user('collision@example.com')
        self.emailoo1 = make_account(self.user, 'emailoo1@gmail.com', sent_folder='Sent1')
        self.emailoo2 = make_account(self.user, 'emailoo2@gmail.com', sent_folder='Sent2')

    def test_no_reply_recorded_when_recipient_never_replies(self):
        """Test 7 -- the literal reported scenario, no reciprocal campaign
        in play. Sanity baseline: sending emailoo2 -> emailoo1 with no
        reply must not produce a false Reply even before the collision
        precondition (test below) is introduced."""
        campaign = make_campaign(self.user, name='One-way')
        msg_id = _msgid()
        make_sent_contact(campaign, self.emailoo2, self.emailoo1.email, msg_id)

        # The campaign's own delivered copy, sitting in emailoo1's synced
        # INBOX (step 1 of a single-step campaign carries no In-Reply-To —
        # see so_smtp.py::build_message, so_drip.py's threading only
        # applies from step 2 onward).
        delivered_copy = _raw_message({
            'From': self.emailoo2.email, 'To': self.emailoo1.email,
            'Subject': 's', 'Message-ID': msg_id,
        }, body='Campaign body.')
        _sync(self.emailoo1, {b'1': delivered_copy})

        self.assertEqual(SOEvent.objects.filter(campaign=campaign, event_type='replied').count(), 0)
        campaign.refresh_from_db()
        self.assertEqual(campaign.total_replied, 0)

    def test_connected_sender_as_recipient_does_not_produce_false_reply(self):
        """Test 8 -- the exact reported bug, reproduced with the precondition
        that makes the OLD (unfixed) code actually misfire: emailoo2 is
        ALSO independently enrolled as a recipient in a second campaign
        (a reciprocal test, or simply importing both of one's own
        addresses into a list — the report's own framing). Without the
        self-owned-address exclusion (Fix 5) and account-scoped matching
        (Fix 6), emailoo1's inbox sync would match the campaign-A delivery
        copy's From (emailoo2) against campaign-B's contact row for
        emailoo2 and record a false reply on campaign B."""
        campaign_a = make_campaign(self.user, name='A: emailoo2 -> emailoo1')
        campaign_b = make_campaign(self.user, name='B: emailoo1 -> emailoo2 (reciprocal)')

        msg_id_a = _msgid()
        make_sent_contact(campaign_a, self.emailoo2, self.emailoo1.email, msg_id_a)
        # The precondition: emailoo2 is ALSO a campaign contact somewhere.
        msg_id_b = _msgid()
        make_sent_contact(campaign_b, self.emailoo1, self.emailoo2.email, msg_id_b)

        # Campaign A's delivered copy lands in emailoo1's own synced inbox.
        delivered_copy = _raw_message({
            'From': self.emailoo2.email, 'To': self.emailoo1.email,
            'Subject': 's', 'Message-ID': msg_id_a,
        }, body='Campaign A body.')
        _sync(self.emailoo1, {b'1': delivered_copy})

        self.assertEqual(SOEvent.objects.filter(event_type='replied').count(), 0,
                         "the campaign's own delivery copy must never be recorded as a reply "
                         "to any campaign, including the reciprocal one")
        campaign_a.refresh_from_db()
        campaign_b.refresh_from_db()
        self.assertEqual(campaign_a.total_replied, 0)
        self.assertEqual(campaign_b.total_replied, 0)

    def test_actual_reply_from_a_connected_account_is_still_recorded(self):
        """Test 9 -- the self-owned-address protection must not block a
        GENUINE reply merely because it comes from a connected account.
        emailoo1 replies for real, with In-Reply-To correctly threading to
        campaign A's actual sent Message-ID -- this hits the strong,
        thread-evidence-backed match, which never consults own_addresses."""
        campaign_a = make_campaign(self.user, name='A: emailoo2 -> emailoo1')
        msg_id_a = _msgid()
        make_sent_contact(campaign_a, self.emailoo2, self.emailoo1.email, msg_id_a)

        # A genuine reply lands in emailoo2's OWN inbox (replies go back to
        # the sender), correctly threaded.
        real_reply = _raw_message({
            'From': self.emailoo1.email, 'To': self.emailoo2.email,
            'Subject': 'Re: s', 'Message-ID': _msgid(),
            'In-Reply-To': msg_id_a, 'References': msg_id_a,
        }, body='Yes, I am interested, please tell me more.')
        _sync(self.emailoo2, {b'1': real_reply})

        self.assertEqual(SOEvent.objects.filter(campaign=campaign_a, event_type='replied').count(), 1)
        campaign_a.refresh_from_db()
        self.assertEqual(campaign_a.total_replied, 1)


class OutOfOfficeTests(TestCase):
    """Fix 9 -- an auto-reply must not create a 'replied' event at all
    (previously it did; only the sequence-stop was skipped)."""

    def setUp(self):
        self.user = make_user('oof@example.com')
        self.account = make_account(self.user, 'oofsender@example.com')
        self.campaign = make_campaign(self.user)

    def test_auto_reply_does_not_create_a_replied_event(self):
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'vacationer@example.com', msg_id)

        oof = _raw_message({
            'From': cc.email, 'To': self.account.email, 'Subject': 'Automatic reply: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
            'Auto-Submitted': 'auto-replied',
        }, body="I'm out of office until Monday.")
        _sync(self.account, {b'1': oof})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_replied, 0)
        # The sequence itself is correctly left un-stopped, as before.
        cc.refresh_from_db()
        self.assertNotEqual(cc.status, 'stopped')
        # Still visible in the Inbox, classified as out_of_office -- the
        # existing separate-storage behavior is preserved.
        convo = SOConversation.objects.filter(campaign_contact=cc).first()
        self.assertIsNotNone(convo)
        self.assertEqual(convo.classification, 'out_of_office')


class MultiStepThreadingTests(TestCase):
    """Fix 7/8 -- a reply to an OLDER step (after later steps have already
    been sent, so cc.message_id no longer holds it) must still match, via
    the per-step SOEvent history rather than only the latest message_id."""

    def setUp(self):
        self.user = make_user('multistep@example.com')
        self.account = make_account(self.user, 'seqsender@example.com')
        self.campaign = make_campaign(self.user)

    def test_reply_to_an_earlier_step_still_matches(self):
        from django.utils.timezone import now

        step1_msg_id = _msgid()
        step2_msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.account, 'prospect@example.com', step1_msg_id)
        # Simulate step 2 having since been sent -- cc.message_id now holds
        # step 2's id, but the per-step SOEvent history for step 1 remains.
        SOCampaignContact.objects.filter(id=cc.id).update(message_id=step2_msg_id, current_step=2)
        SOEvent.objects.create(
            campaign=self.campaign, prospect=cc.prospect, account=self.account,
            message_id=step2_msg_id, email=cc.email, event_type='sent',
            metadata={'step': 2}, step_order=2,
        )

        # The prospect replies to the FIRST email, not the most recent one.
        late_reply = _raw_message({
            'From': cc.email, 'To': self.account.email, 'Subject': 'Re: s',
            'Message-ID': _msgid(), 'In-Reply-To': step1_msg_id, 'References': step1_msg_id,
        }, body='Sorry for the late reply to your first email!')
        _sync(self.account, {b'1': late_reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 1)


@override_settings(ENABLE_EMAIL_TRACKING=True)
class ReplyToHeaderTests(TestCase):
    """Outgoing side: SOCampaign.reply_to -> build_message() -> the actual
    Reply-To MIME header on a real send_next_step() send. Verifies the
    header appears on the wire (inspecting the raw bytes handed to
    smtplib.sendmail), not just that build_message() accepts the param."""

    def setUp(self):
        self.user = make_user('reply_to_header@example.com')
        self.account = make_account(self.user, 'emailoo2@gmail.com')
        self.campaign = make_campaign(self.user)
        self.prospect = SOProspect.objects.create(
            user_id=self.user.id, email='john@gmail.com', first_name='John', last_name='Doe',
            status='subscribed',
        )
        self.cc = SOCampaignContact.objects.create(
            campaign=self.campaign, prospect=self.prospect, email=self.prospect.email,
            account=self.account, status='sending', current_step=1, attempts=0,
        )

    def _sent_message(self):
        """Runs send_next_step() for real (SMTP faked), returns the parsed
        MIME message that was actually handed to sendmail()."""
        import email as email_pkg
        mock_server = MagicMock()
        mock_server.sendmail.return_value = {}
        with patch('Email_validate_app.services.so_smtp.open_smtp', return_value=mock_server):
            result = so_drip.send_next_step(self.cc)
        self.assertTrue(result, 'send_next_step should have succeeded')
        raw = mock_server.sendmail.call_args[0][2]
        return email_pkg.message_from_bytes(raw)

    def test_reply_to_header_present_when_campaign_has_one(self):
        self.campaign.reply_to = 'sales@company.com'
        self.campaign.save(update_fields=['reply_to'])

        msg = self._sent_message()

        # From includes the display name (send_next_step's from_nm falls
        # back to account.display_name, which make_account() sets to the
        # local-part of the email) -- the address itself must still be
        # exactly account.email.
        self.assertIn(self.account.email, msg['From'])
        self.assertEqual(msg['To'], self.cc.email)
        self.assertEqual(msg['Reply-To'], 'sales@company.com')

    def test_no_reply_to_header_when_campaign_has_none(self):
        """Scenario A -- regression guard: an empty/blank reply_to (the
        model default) must never emit a Reply-To header at all."""
        self.assertEqual(self.campaign.reply_to, '')

        msg = self._sent_message()

        self.assertIsNone(msg['Reply-To'])
        self.assertIn(self.account.email, msg['From'])

    def test_other_headers_unaffected_by_reply_to(self):
        """Adding Reply-To must not disturb Message-ID/In-Reply-To/
        List-Unsubscribe -- set up a second step so In-Reply-To is
        populated too."""
        self.campaign.reply_to = 'sales@company.com'
        self.campaign.save(update_fields=['reply_to'])
        prior_msg_id = _msgid()
        self.cc.message_id = prior_msg_id
        self.cc.save(update_fields=['message_id'])

        msg = self._sent_message()

        self.assertTrue(msg['Message-ID'])
        self.assertEqual(msg['In-Reply-To'], prior_msg_id)
        self.assertEqual(msg['References'], prior_msg_id)
        self.assertTrue(msg['List-Unsubscribe'])
        self.assertEqual(msg['Reply-To'], 'sales@company.com')


class ReplyToMailboxTrackingTests(TestCase):
    """Incoming side: a reply to a campaign with SOCampaign.reply_to set
    arrives at that DIFFERENT mailbox (not the account that actually sent
    the campaign) and must still be matched to the correct
    SOCampaignContact/campaign/conversation, exactly like the exact
    scenario from the investigation report:
      Sending account: emailoo2@gmail.com
      Reply-To:        sales@company.com
      Prospect:        john@gmail.com
    """

    def setUp(self):
        self.user = make_user('reply_to_mailbox@example.com')
        self.sender = make_account(self.user, 'emailoo2@gmail.com', sent_folder='Sent1')
        self.reply_mailbox = make_account(self.user, 'sales@company.com', sent_folder='Sent2')
        self.campaign = make_campaign(self.user, name='Reply-To Campaign')
        self.campaign.reply_to = self.reply_mailbox.email
        self.campaign.save(update_fields=['reply_to'])

    def test_reply_landing_in_the_reply_to_mailbox_is_matched(self):
        """Scenario B, tier 1a: strong Message-ID match, but the incoming
        mailbox is NOT the sending account -- it's the campaign's own
        configured Reply-To mailbox instead."""
        msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.sender, 'john@gmail.com', msg_id)

        reply = _raw_message({
            'From': 'john@gmail.com', 'To': self.reply_mailbox.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Sounds great, tell me more!')
        # Synced from the REPLY-TO mailbox, not the sending account.
        _sync(self.reply_mailbox, {b'1': reply})

        event = SOEvent.objects.filter(campaign=self.campaign, event_type='replied').first()
        self.assertIsNotNone(event, 'the reply must be matched even though it arrived at a different mailbox')
        # _record_once must still attribute the event to the ORIGINAL
        # sending account/campaign, not the mailbox that happened to sync it.
        self.assertEqual(event.account_id, self.sender.id)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_replied, 1)

        # SOCampaignContact resolves correctly too.
        cc.refresh_from_db()
        conversation = SOConversation.objects.filter(campaign_contact=cc).first()
        self.assertIsNotNone(conversation, 'a conversation must be created/updated for the matched contact')
        self.assertEqual(conversation.campaign_id, self.campaign.id)

    def test_reply_to_an_older_step_via_reply_to_mailbox_still_matches(self):
        """Scenario B, tier 1b: the SOEvent 'sent' fallback for an older
        step must ALSO honor the Reply-To mailbox, not just tier 1a."""
        step1_msg_id = _msgid()
        step2_msg_id = _msgid()
        cc = make_sent_contact(self.campaign, self.sender, 'john@gmail.com', step1_msg_id)
        SOCampaignContact.objects.filter(id=cc.id).update(message_id=step2_msg_id, current_step=2)
        SOEvent.objects.create(
            campaign=self.campaign, prospect=cc.prospect, account=self.sender,
            message_id=step2_msg_id, email=cc.email, event_type='sent',
            metadata={'step': 2}, step_order=2,
        )

        late_reply = _raw_message({
            'From': 'john@gmail.com', 'To': self.reply_mailbox.email, 'Subject': 'Re: s',
            'Message-ID': _msgid(), 'In-Reply-To': step1_msg_id, 'References': step1_msg_id,
        }, body='Sorry for the late reply to your first email!')
        _sync(self.reply_mailbox, {b'1': late_reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 1)

    def test_reply_at_an_unconfigured_mailbox_is_not_matched(self):
        """Negative control: a THIRD connected account (not the sender, not
        the campaign's configured Reply-To) must NOT match, even though it
        quotes a real Message-ID and belongs to the same user."""
        other_account = make_account(self.user, 'unrelated@gmail.com', sent_folder='Sent3')
        msg_id = _msgid()
        make_sent_contact(self.campaign, self.sender, 'john@gmail.com', msg_id)

        reply = _raw_message({
            'From': 'john@gmail.com', 'To': other_account.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='This should not match.')
        _sync(other_account, {b'1': reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_replied, 0)

    def test_cross_tenant_reply_to_string_collision_is_not_matched(self):
        """Security guard: a DIFFERENT user's connected account that happens
        to share the same email address as this campaign's reply_to string
        must never be able to claim this campaign's reply -- matching is
        scoped to campaign.user_id == syncing_account.user_id."""
        other_user = make_user('other_tenant@example.com')
        # A different tenant's own account, coincidentally the same address
        # as self.campaign's configured reply_to.
        other_users_mailbox = SOEmailAccount.objects.create(
            user_id=other_user.id, provider='google', display_name='sales',
            email=self.reply_mailbox.email, smtp_host='smtp.test', smtp_port=587,
            imap_host='imap.test', imap_port=993, username=self.reply_mailbox.email,
            password='x', daily_limit=50, status='connected', sent_folder='SentX',
        )
        msg_id = _msgid()
        make_sent_contact(self.campaign, self.sender, 'john@gmail.com', msg_id)

        reply = _raw_message({
            'From': 'john@gmail.com', 'To': other_users_mailbox.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='This must not be attributed to the wrong tenant.')
        _sync(other_users_mailbox, {b'1': reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)

    def test_no_reply_to_configured_still_matches_the_sending_account_only(self):
        """Scenario A, replayed inside this class's setup: with reply_to
        cleared, behavior must be identical to the pre-existing,
        same-account-only matching -- no regression from the relaxed
        query."""
        self.campaign.reply_to = ''
        self.campaign.save(update_fields=['reply_to'])
        msg_id = _msgid()
        make_sent_contact(self.campaign, self.sender, 'john@gmail.com', msg_id)

        # A reply landing at the (no longer configured) reply_mailbox must
        # NOT match once reply_to is cleared.
        reply = _raw_message({
            'From': 'john@gmail.com', 'To': self.reply_mailbox.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Should not match once reply_to is cleared.')
        _sync(self.reply_mailbox, {b'1': reply})
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)

        # The SAME reply landing at the actual sending account still works.
        reply2 = _raw_message({
            'From': 'john@gmail.com', 'To': self.sender.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Should match at the real sending account.')
        _sync(self.sender, {b'2': reply2})
        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 1)

    def test_syncing_the_same_reply_twice_does_not_duplicate(self):
        """Idempotency (_record_once): re-syncing the same message must
        never create a second 'replied' SOEvent or double-increment
        total_replied."""
        msg_id = _msgid()
        make_sent_contact(self.campaign, self.sender, 'john@gmail.com', msg_id)

        reply = _raw_message({
            'From': 'john@gmail.com', 'To': self.reply_mailbox.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Interested!')

        _sync(self.reply_mailbox, {b'1': reply})
        _sync(self.reply_mailbox, {b'1': reply})   # same message, synced again

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 1)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.total_replied, 1)

    def test_weak_fallback_stays_same_account_only_even_with_reply_to_configured(self):
        """Explicit scope boundary (per design): a headerless message (no
        In-Reply-To/References at all) landing in the Reply-To mailbox is
        NOT resolved by the weak, address-only fallback -- that fallback is
        deliberately left same-account-only. Only the strong,
        Message-ID-backed tiers understand the Reply-To mailbox."""
        make_sent_contact(self.campaign, self.sender, 'john@gmail.com', _msgid())

        headerless_reply = _raw_message({
            'From': 'john@gmail.com', 'To': self.reply_mailbox.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(),
            # No In-Reply-To / References at all.
        }, body='Replying from a client that drops threading headers.')
        _sync(self.reply_mailbox, {b'1': headerless_reply})

        self.assertEqual(SOEvent.objects.filter(campaign=self.campaign, event_type='replied').count(), 0)

    def test_matching_is_provider_agnostic(self):
        """_mailbox_is_valid_for_reply() never reads provider/smtp_host/
        imap_host -- only .id/.user_id/.email -- so a Reply-To mailbox on a
        DIFFERENT provider than the sender must match exactly the same way.
        Rebuilds sender/reply-to as realistic cross-provider accounts
        (Gmail sender, Microsoft 365 Reply-To) rather than relying on the
        generic smtp.test/imap.test values make_account() otherwise uses
        for every account in this file."""
        cross_provider_user = make_user('cross_provider@example.com')
        gmail_sender = SOEmailAccount.objects.create(
            user_id=cross_provider_user.id, provider='google', display_name='Sales (Gmail)',
            email='emailoo2@gmail.com', smtp_host='smtp.gmail.com', smtp_port=587,
            imap_host='imap.gmail.com', imap_port=993, username='emailoo2@gmail.com',
            password='x', daily_limit=50, status='connected', sent_folder='[Gmail]/Sent Mail',
        )
        m365_reply_to = SOEmailAccount.objects.create(
            user_id=cross_provider_user.id, provider='microsoft', display_name='Sales (M365)',
            email='sales@company.com', smtp_host='smtp.office365.com', smtp_port=587,
            imap_host='outlook.office365.com', imap_port=993, username='sales@company.com',
            password='x', daily_limit=50, status='connected', sent_folder='Sent Items',
        )
        campaign = make_campaign(cross_provider_user, name='Cross-Provider Campaign')
        campaign.reply_to = m365_reply_to.email
        campaign.save(update_fields=['reply_to'])

        # Prospect on a third provider (Yahoo) -- irrelevant to matching
        # (the prospect has no SOEmailAccount at all), included only to
        # mirror the real-world scenario end to end.
        msg_id = _msgid()
        make_sent_contact(campaign, gmail_sender, 'prospect@yahoo.com', msg_id)

        reply = _raw_message({
            'From': 'prospect@yahoo.com', 'To': m365_reply_to.email, 'Subject': 'Re: Hello',
            'Message-ID': _msgid(), 'In-Reply-To': msg_id, 'References': msg_id,
        }, body='Replying from Yahoo to your Microsoft 365 Reply-To address.')
        _sync(m365_reply_to, {b'1': reply})

        event = SOEvent.objects.filter(campaign=campaign, event_type='replied').first()
        self.assertIsNotNone(event, 'a Gmail sender / Microsoft 365 Reply-To / Yahoo prospect '
                                     'combination must match exactly like same-provider accounts do')
        self.assertEqual(event.account_id, gmail_sender.id)
        campaign.refresh_from_db()
        self.assertEqual(campaign.total_replied, 1)
