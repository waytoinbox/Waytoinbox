"""
Gmail API interaction for Warmup's receiver-side checker. This is Gmail-API
based mailbox label manipulation — it is NOT browser automation, and
clearing the UNREAD label is tracked purely as a mailbox-state fact, not a
claim about human engagement or sender-reputation effect.

Reuses the exact interaction shape proven in the user's own reference
script: messages().list(q='subject:"<id>"') -> messages().get(format=
'metadata') -> inspect labelIds -> messages().modify(...). The one
functional difference from that script: it also rescues Spam -> Inbox (per
explicit instruction), but never overwrites the ORIGINAL landing_location —
that distinction is enforced by the caller (tasks/warmup.py::warmup_check_one),
not here; this module only reports what it found and does what it's told.
"""

import logging

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from Email_validate_app.services.warmup_crypto import decrypt_token

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_URI = 'https://oauth2.googleapis.com/token'


class ReceiverAuthError(Exception):
    """Refresh token invalid/revoked — receiver needs to be reconnected via
    the OAuth flow. Callers should mark the receiver 'revoked', not treat
    this as a transient/retryable failure."""


class GmailApiError(Exception):
    """Transient Gmail API failure (rate limit, network, 5xx) — callers
    should treat this as retryable with backoff, distinct from ReceiverAuthError."""


def _oauth_client_config():
    import os
    client_id     = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip()
    client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
    if not client_id or not client_secret:
        raise EnvironmentError(
            'GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set in .env. '
            'Create a Web application OAuth client in Google Cloud Console first.'
        )
    return client_id, client_secret


def get_gmail_service(receiver):
    """Builds an authenticated Gmail API service for `receiver` (a
    WarmupReceiverAccount), always refreshing a fresh access token from the
    stored encrypted refresh token — no access token is ever cached, so
    there is only one long-lived secret to protect per receiver."""
    client_id, client_secret = _oauth_client_config()

    try:
        refresh_token = decrypt_token(receiver.refresh_token_encrypted)
    except ValueError as exc:
        raise ReceiverAuthError(str(exc)) from exc

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise ReceiverAuthError(f'Refresh token rejected for {receiver.email}: {exc}') from exc

    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def find_warmup_message(service, identifier: str) -> dict | None:
    """Searches for the warmup email by its unique identifier (embedded in
    the subject) and returns {'id': ..., 'labelIds': [...]}, or None if no
    match yet. format='metadata' — no need to fetch the full body.

    includeSpamTrash=True is required here — Gmail's messages().list
    excludes SPAM and TRASH from search results by default, which would
    make a spam-foldered warmup email indistinguishable from one that
    never arrived at all (both return no match here, falling through to
    the caller's not-found/retry path). classify_landing() below already
    correctly branches on the SPAM label once a message IS found; this is
    what makes that message actually reachable to search for in the first
    place."""
    try:
        result = service.users().messages().list(
            userId='me', q=f'subject:"{identifier}"', maxResults=5, includeSpamTrash=True,
        ).execute()
    except HttpError as exc:
        if exc.resp is not None and exc.resp.status in (401, 403):
            raise ReceiverAuthError(f'Gmail API rejected the request: {exc}') from exc
        raise GmailApiError(str(exc)) from exc

    messages = result.get('messages', [])
    if not messages:
        return None
    if len(messages) > 1:
        logger.warning('warmup_receiver: %d matches for identifier %s, using the first', len(messages), identifier)

    try:
        full = service.users().messages().get(
            userId='me', id=messages[0]['id'], format='metadata', metadataHeaders=['Subject'],
        ).execute()
    except HttpError as exc:
        if exc.resp is not None and exc.resp.status in (401, 403):
            raise ReceiverAuthError(f'Gmail API rejected the request: {exc}') from exc
        raise GmailApiError(str(exc)) from exc

    return {'id': full['id'], 'labelIds': full.get('labelIds', [])}


def classify_landing(label_ids: list) -> str:
    """'inbox' / 'spam' / 'other'. 'not_found' is decided by the caller
    (when find_warmup_message returns None), not here."""
    if 'SPAM' in label_ids:
        return 'spam'
    if 'INBOX' in label_ids:
        return 'inbox'
    return 'other'


def mark_as_read(service, message_id: str) -> None:
    """Removes UNREAD only — call only when UNREAD was actually present."""
    try:
        service.users().messages().modify(
            userId='me', id=message_id, body={'removeLabelIds': ['UNREAD']},
        ).execute()
    except HttpError as exc:
        if exc.resp is not None and exc.resp.status in (401, 403):
            raise ReceiverAuthError(f'Gmail API rejected the request: {exc}') from exc
        raise GmailApiError(str(exc)) from exc


def rescue_from_spam(service, message_id: str, was_unread: bool) -> None:
    """Moves a message out of Spam: removes SPAM, adds INBOX, and — in the
    same API call — also removes UNREAD if it was present. One combined
    modify() call rather than two, since `was_unread` is already known from
    the labelIds fetched during classify_landing (no extra read needed)."""
    remove = ['SPAM'] + (['UNREAD'] if was_unread else [])
    try:
        service.users().messages().modify(
            userId='me', id=message_id, body={'removeLabelIds': remove, 'addLabelIds': ['INBOX']},
        ).execute()
    except HttpError as exc:
        if exc.resp is not None and exc.resp.status in (401, 403):
            raise ReceiverAuthError(f'Gmail API rejected the request: {exc}') from exc
        raise GmailApiError(str(exc)) from exc


def get_profile_email(service) -> str:
    """Used only during the OAuth callback, to bind the connected receiver
    row to whatever email address Google actually authenticated — never
    trust a user-typed email for this."""
    try:
        profile = service.users().getProfile(userId='me').execute()
    except HttpError as exc:
        raise GmailApiError(str(exc)) from exc
    return profile['emailAddress']
