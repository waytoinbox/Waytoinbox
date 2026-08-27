"""
so_inbox.py
-----------
Support code for the Sales Outreach Inbox — replying to a prospect from inside
a conversation, merging a conversation's full timeline for display, and the
shared unsubscribe side-effect (also used by the tracking-link view).

Sequence sends live in services/so_drip.py; this module only ever sends a
single, direct, untracked message (no open pixel, no click-wrapping, no
unsubscribe footer) — a manual reply is correspondence, not a bulk send.
"""

import hashlib
import html as html_lib
import logging
import re
import smtplib
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart

from django.utils.timezone import now

logger = logging.getLogger(__name__)


def _site_url():
    from django.conf import settings
    return getattr(settings, 'SITE_URL', 'https://waytoinbox.com').rstrip('/')


def cc_thread_key(campaign_contact_id):
    """The thread_key for a campaign-linked conversation. Must stay byte-identical
    to migrations/0111_so_inbox_unibox.py's backfill SQL (CONCAT('cc:', ...))."""
    return f'cc:{campaign_contact_id}'


def account_thread_key(account_id, counterpart_email):
    """The thread_key for a general (non-campaign) conversation — grouped by
    account + the other party's address, since there is no campaign_contact to
    key off of."""
    return f'acct:{account_id}:{(counterpart_email or "").strip().lower()}'


def get_or_create_compose_conversation(account, to_email, subject):
    """Find-or-create the conversation a freshly-composed message belongs to.
    Uses the same account_thread_key identity a later inbound reply or
    Sent-folder scan would resolve to, so composing to someone you already
    have a thread with continues it instead of forking a duplicate — same
    find-or-create shape as the general-mail path in so_imap.py."""
    from Email_validate_app.models import SOConversation, SOProspect

    to_email = (to_email or '').strip().lower()
    prospect = SOProspect.objects.filter(
        user_id=account.user_id, email__iexact=to_email, deleted_at__isnull=True,
    ).first()
    conversation, _created = SOConversation.objects.get_or_create(
        thread_key=account_thread_key(account.id, to_email),
        defaults={'account': account, 'email': to_email, 'subject': subject or '', 'prospect': prospect},
    )
    return conversation


_STYLE_SCRIPT_RE = re.compile(r'<(style|script)\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r'\s+')


def _preview_text(html_str):
    """Plain-text preview for the conversation list. django.utils.html.strip_tags
    only removes tags, not the CONTENT of <style>/<script> blocks — an HTML
    marketing email's embedded CSS would otherwise leak into the preview
    looking like raw code. Strip those blocks first, then collapse whitespace
    and decode entities (&nbsp; etc.) so the result reads as plain text."""
    from django.utils.html import strip_tags

    if not html_str:
        return ''
    cleaned = _STYLE_SCRIPT_RE.sub(' ', html_str)
    cleaned = strip_tags(cleaned)
    cleaned = html_lib.unescape(cleaned)
    return _WHITESPACE_RE.sub(' ', cleaned).strip()


def _headerless_fingerprint(account_id, direction, from_email, to_email, subject, body_text, body_html, sent_at):
    """Deterministic fallback dedup key for a message that had no Message-ID
    header — everything a Message-ID would otherwise let us tell two emails
    apart by, hashed together:

    - account + direction: scopes the fingerprint to this one mailbox side of
      one conversation direction — never compared across accounts (a
      different account's identical-looking message gets its own uniqueness
      scope entirely, via the (account, headerless_fingerprint) constraint).
    - normalized from/to/subject: the visible envelope identity.
    - normalized body content (via _preview_text's HTML/whitespace
      normalization, not the 300-char display preview — the full normalized
      text, so near-identical-looking-but-different bodies still diverge).
    - the exact parsed sent_at (from the email's own Date header, via
      _parse_message_date in so_imap.py) — included un-rounded specifically
      so two genuinely separate emails that happen to share every other
      field (sender, recipient, subject, body) still get different
      fingerprints whenever their timestamps differ, rather than collapsing
      into one. The SAME physical message re-synced later carries the exact
      same Date header every time, so the fingerprint is stable across
      repeated IMAP passes.

    Fields are joined with an ASCII unit separator (not plain concatenation)
    so e.g. from_email='a'+subject='bc' can never hash the same as
    from_email='ab'+subject='c'.
    """
    body = (body_text or '').strip() or (_preview_text(body_html) if body_html else '')
    parts = [
        str(account_id or ''),
        direction or '',
        (from_email or '').strip().lower(),
        (to_email or '').strip().lower(),
        _WHITESPACE_RE.sub(' ', (subject or '').strip()).lower(),
        body,
        sent_at.isoformat() if sent_at else '',
    ]
    raw = '\x1f'.join(parts)
    return hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()


def upsert_conversation_message(*, thread_key, account, direction, subject, body_html, body_text,
                                 from_email, to_email, message_id, in_reply_to='',
                                 has_attachments=False, sent_at=None, is_sequence_step=False,
                                 campaign_contact=None, campaign=None, prospect=None,
                                 counterpart_email, classification_if_new=''):
    """Idempotent upsert used by every write-path that records a real email into
    the Inbox's conversation/message store (campaign sequence sends, manual
    replies via IMAP-observed Sent-folder mail, campaign replies, and general
    non-campaign mail) — get-or-create the conversation by thread_key, then
    insert the message iff (account, message_id) hasn't been recorded before.

    Returns (conversation, message_or_None) — message is None when this exact
    physical email was already recorded (e.g. a campaign send later re-observed
    via a Sent-folder scan) — not an error, just nothing new to do.
    """
    from django.db import IntegrityError, transaction
    from Email_validate_app.models import SOConversation, SOMessage

    message_id = (message_id or '').strip() or None
    sent_at = sent_at or now()

    # Messages with a real Message-ID keep using that exact existing dedup
    # path, unchanged. Only a headerless message computes a fingerprint at
    # all — message_id and headerless_fingerprint are never both set on the
    # same row.
    fingerprint = None
    if not message_id:
        fingerprint = _headerless_fingerprint(
            account.id if account else None, direction, from_email, to_email,
            subject, body_text, body_html, sent_at,
        )

    # This physical email may already be recorded under a DIFFERENT
    # conversation (e.g. a campaign send re-observed via a Sent-folder scan,
    # or — as found in this table's existing data — a single reply whose
    # References header matched more than one campaign contact at once).
    # Catch that BEFORE creating a conversation for it.
    if message_id and SOMessage.objects.filter(account=account, message_id=message_id).exists():
        conv = SOConversation.objects.filter(thread_key=thread_key).first()
        return conv, None
    if fingerprint and SOMessage.objects.filter(account=account, headerless_fingerprint=fingerprint).exists():
        conv = SOConversation.objects.filter(thread_key=thread_key).first()
        return conv, None

    conversation, _created = SOConversation.objects.get_or_create(
        thread_key=thread_key,
        defaults={
            'campaign_contact': campaign_contact, 'campaign': campaign,
            'prospect': prospect, 'account': account,
            'email': counterpart_email, 'subject': subject,
        },
    )

    preview = (_preview_text(body_html) if body_html else (body_text or '').strip())[:300]
    try:
        with transaction.atomic():
            message = SOMessage.objects.create(
                conversation=conversation, account=account, direction=direction,
                is_sequence_step=is_sequence_step,
                subject=subject, body_html=body_html, body_text=(body_text or '')[:5000],
                from_email=from_email, to_email=to_email,
                message_id=message_id, headerless_fingerprint=fingerprint, in_reply_to=in_reply_to,
                has_attachments=has_attachments, sent_at=sent_at,
            )
    except IntegrityError:
        # Concurrent-sync race on the same (account, message_id) OR the same
        # (account, headerless_fingerprint) — the other writer won, nothing
        # more to do here. This is the actual concurrency-safety mechanism
        # for both dedup paths: the app-level .exists() checks above are a
        # cheap common-case shortcut, but only the unique_together
        # constraint (caught here) is safe against two workers racing to
        # insert the same physical message at the same instant.
        return conversation, None

    fields = {
        'last_message_at': sent_at, 'last_message_preview': preview,
        'last_message_direction': direction,
    }
    if direction == 'inbound':
        fields['is_unread'] = True
    SOConversation.objects.filter(id=conversation.id).update(**fields)
    if classification_if_new:
        SOConversation.objects.filter(id=conversation.id, classification='').update(
            classification=classification_if_new
        )
    return conversation, message


def unsubscribe_contact(cc):
    """Shared unsubscribe side-effect — used by both the tracking-link view
    (views/so_tracking.py) and the Inbox's Unsubscribe action, so there is one
    behavior, not two copies that could drift."""
    from django.db.models import F
    from Email_validate_app.models import SOProspect, SOEvent, SOCampaign
    from Email_validate_app.services.so_drip import stop as _stop_sequence

    SOProspect.objects.filter(
        user_id=cc.campaign.user_id, email=cc.email, deleted_at__isnull=True,
    ).update(status='unsubscribed')
    SOEvent.objects.create(
        campaign=cc.campaign, prospect=cc.prospect, account_id=cc.account_id,
        message_id=cc.message_id, email=cc.email, event_type='unsubscribed',
    )
    SOCampaign.objects.filter(id=cc.campaign_id).update(total_unsubscribed=F('total_unsubscribed') + 1)
    _stop_sequence(cc, 'unsubscribed')


def get_thread_timeline(conversation):
    """Merge SOMessage + SOConversationNote + relevant SOEvent (opens/clicks)
    into one chronological list of plain dicts, ready for JSON serialization."""
    from Email_validate_app.models import SOMessage, SOConversationNote, SOEvent

    items = []
    for m in SOMessage.objects.filter(conversation=conversation).order_by('created_at'):
        items.append({
            'kind': 'message', 'id': m.id, 'direction': m.direction,
            'is_sequence_step': m.is_sequence_step, 'subject': m.subject,
            'body_html': m.body_html, 'body_text': m.body_text,
            'from_email': m.from_email, 'to_email': m.to_email,
            'has_attachments': m.has_attachments,
            'timestamp': (m.sent_at or m.created_at).isoformat(),
        })
    for n in SOConversationNote.objects.filter(conversation=conversation).select_related('user').order_by('created_at'):
        items.append({
            'kind': 'note', 'id': n.id, 'body': n.body,
            'author': n.user.user_name or n.user.user_email,
            'timestamp': n.created_at.isoformat(),
        })
    for e in SOEvent.objects.filter(
        campaign_id=conversation.campaign_id, email=conversation.email,
        event_type__in=('opened', 'clicked'),
    ).order_by('created_at'):
        items.append({
            'kind': 'event', 'id': e.id, 'event_type': e.event_type,
            'metadata': e.metadata, 'timestamp': e.created_at.isoformat(),
        })
    items.sort(key=lambda x: x['timestamp'])
    return items


def _split_addresses(raw):
    """'a@x.com, b@y.com' -> ['a@x.com', 'b@y.com'], dropping blanks."""
    return [a.strip() for a in (raw or '').split(',') if a.strip()]


def send_reply(conversation, body_html, attachments=None, to_email=None, forward=False,
                subject=None, cc_email='', bcc_email=''):
    """Send a manual reply — or, with forward=True, a forward to someone
    outside the conversation — from the Inbox composer. `attachments` is an
    optional list of (filename, content_bytes, mimetype) tuples. `to_email`,
    `subject`, `cc_email` and `bcc_email` are all editable in the composer;
    each falls back to a sensible default when left blank.

    Returns the created outbound SOMessage on success. Raises on failure —
    callers are the JSON view layer, which turns that into an error response.
    """
    from Email_validate_app.models import SOMessage
    from Email_validate_app.services.so_smtp import build_message, open_smtp

    account = conversation.account
    if not account or account.deleted_at:
        raise ValueError('This conversation has no valid sender account.')

    to_email = (to_email or '').strip() or conversation.email
    cc_email = ', '.join(_split_addresses(cc_email))
    bcc_email = ', '.join(_split_addresses(bcc_email))

    subject = (subject or '').strip()
    if not subject:
        subject = conversation.subject or ''
        if forward:
            if not subject.lower().startswith('fwd:'):
                subject = f'Fwd: {subject}' if subject else 'Fwd:'
        elif not subject.lower().startswith('re:'):
            subject = f'Re: {subject}' if subject else 'Re:'

    # A forward goes to someone outside this conversation — referencing the
    # internal reply-chain's Message-IDs to a third party would be meaningless
    # (and leaks internal thread wiring), so forwards start a fresh thread.
    in_reply_to = None
    if not forward:
        last_inbound = SOMessage.objects.filter(
            conversation=conversation, direction='inbound',
        ).order_by('-sent_at', '-created_at').first()
        in_reply_to = last_inbound.message_id if last_inbound and last_inbound.message_id else (
            conversation.campaign_contact.message_id if conversation.campaign_contact else None
        )

    msg_id  = f'<{uuid.uuid4()}@{account.smtp_host}>'
    from_nm = account.display_name or account.email
    sent_at = now()

    msg = build_message(from_nm, account.email, to_email, subject, body_html, '', msg_id,
                        in_reply_to=in_reply_to, cc_email=cc_email)

    if attachments:
        outer = MIMEMultipart('mixed')
        for key, val in msg.items():
            outer[key] = val
        for part in msg.get_payload():
            outer.attach(part)
        for filename, content, mimetype in attachments:
            subtype = mimetype.split('/', 1)[1] if mimetype and '/' in mimetype else 'octet-stream'
            part = MIMEApplication(content, _subtype=subtype)
            part.add_header('Content-Disposition', 'attachment', filename=filename)
            outer.attach(part)
        msg = outer

    # The SMTP envelope recipient list is every actual recipient (to + cc +
    # bcc) — Bcc addresses only ever appear here, never as a message header.
    all_recipients = [to_email] + _split_addresses(cc_email) + _split_addresses(bcc_email)

    server = open_smtp(account)
    try:
        refused = server.sendmail(account.email, all_recipients, msg.as_bytes())
        if refused and to_email in refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        try:
            server.quit()
        except Exception:
            pass

    preview = _preview_text(body_html)[:300]

    message = SOMessage.objects.create(
        conversation=conversation, account=account, direction='outbound', is_sequence_step=False,
        subject=subject, body_html=body_html, body_text=preview,
        from_email=account.email, to_email=to_email, cc_email=cc_email, bcc_email=bcc_email,
        message_id=msg_id, in_reply_to=in_reply_to or '',
        has_attachments=bool(attachments), sent_at=sent_at,
    )
    conversation.__class__.objects.filter(id=conversation.id).update(
        last_message_at=sent_at, last_message_preview=preview, last_message_direction='outbound',
    )
    logger.info('so_inbox: reply sent for conversation %s via account %s', conversation.id, account.id)
    return message
