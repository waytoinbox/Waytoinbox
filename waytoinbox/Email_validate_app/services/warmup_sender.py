"""
Warmup email sending — pure reuse of the existing SO SMTP infrastructure
(services/so_smtp.py), no new SMTP code. This module only builds warmup-
specific content and interprets send outcomes.

No content-authoring UI in v1 — subject/body come from a small built-in
template set with the WarmupMessage.identifier embedded in the subject
(the receiver-side Gmail search key). A sequence-variant-style editor is a
clean future addition if wanted.
"""

import uuid

from django.utils.timezone import now

from Email_validate_app.services.so_smtp import build_message, open_smtp

_TEMPLATES = [
    {
        'subject': 'Quick question',
        'body': (
            '<p>Hi,</p>'
            '<p>Just checking in — hope things are going well on your end.</p>'
            '<p>Talk soon.</p>'
        ),
    },
    {
        'subject': 'Following up',
        'body': (
            '<p>Hello,</p>'
            '<p>Wanted to follow up and see how everything is progressing.</p>'
            '<p>Let me know if there is anything you need.</p>'
        ),
    },
    {
        'subject': 'Touching base',
        'body': (
            '<p>Hi there,</p>'
            '<p>Touching base — no action needed, just staying in the loop.</p>'
            '<p>Best.</p>'
        ),
    },
]


def build_warmup_content(identifier: str) -> tuple[str, str]:
    """Returns (subject, html_body). The identifier is embedded in the
    subject so the receiver-side Gmail search (q=subject:"...") can find
    this exact message reliably — see warmup_receiver.py::find_warmup_message."""
    template = _TEMPLATES[hash(identifier) % len(_TEMPLATES)]
    subject = f"{template['subject']} — {identifier}"
    return subject, template['body']


def send_warmup_email(message) -> None:
    """Sends `message` (a WarmupMessage row) via its sender_account's SMTP
    connection. Mutates and saves `message` in place with the outcome —
    callers (the Celery task) are responsible for the claim/status
    transitions around this call, not this function.

    Raises on failure so the caller can distinguish terminal
    (SMTPAuthenticationError) from transient errors and decide whether to
    retry — this function itself does not implement retry logic."""
    account = message.sender_account
    if account is None:
        raise ValueError('WarmupMessage has no sender_account (deleted?) — cannot send.')

    subject, html_body = build_warmup_content(message.identifier)
    msg_id = f'<{uuid.uuid4()}@{account.smtp_host}>'

    mime_msg = build_message(
        from_name=account.display_name or '',
        from_email=account.email,
        to_email=message.receiver_email,
        subject=subject,
        html=html_body,
        unsub_url='',   # warmup emails carry no unsubscribe link
        msg_id=msg_id,
    )

    server = open_smtp(account)
    try:
        server.sendmail(account.email, message.receiver_email, mime_msg.as_bytes())
    finally:
        try:
            server.quit()
        except Exception:
            pass

    message.subject    = subject
    message.message_id = msg_id
    message.sent_at     = now()
