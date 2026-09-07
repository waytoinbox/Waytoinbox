"""
Warmup email sending — pure reuse of the existing SO SMTP infrastructure
(services/so_smtp.py), no new SMTP code. This module only builds warmup-
specific content and interprets send outcomes.

Subject/body come from a small built-in template set with the
WarmupMessage.identifier embedded in the subject (the receiver-side Gmail
search key) — unless the sending account has saved its own custom content
via Edit Settings' Warmup tab (SOEmailAccountWarmup.warmup_subject/
warmup_body), in which case that's used instead. Both fields blank (the
default for every account created before this existed) means "use the
built-in templates," so nothing changes for an account that never sets
custom content.
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


def build_warmup_content(identifier: str, account=None) -> tuple[str, str]:
    """Returns (subject, html_body). The identifier is embedded in the
    subject so the receiver-side Gmail search (q=subject:"...") can find
    this exact message reliably — see warmup_receiver.py::find_warmup_message.

    account's own warmup_subject/warmup_body (SOEmailAccountWarmup) are
    used only when BOTH are set — matching edit_warmup_content's own
    validation, which never persists one without the other — otherwise
    this falls back to the built-in _TEMPLATES rotation exactly as before
    this parameter existed."""
    warmup = getattr(account, 'warmup', None) if account is not None else None
    if warmup and warmup.warmup_subject and warmup.warmup_body:
        subject_base, body = warmup.warmup_subject, warmup.warmup_body
    else:
        template = _TEMPLATES[hash(identifier) % len(_TEMPLATES)]
        subject_base, body = template['subject'], template['body']

    subject = f"{subject_base} — {identifier}"
    return subject, body


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

    subject, html_body = build_warmup_content(message.identifier, account)
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
