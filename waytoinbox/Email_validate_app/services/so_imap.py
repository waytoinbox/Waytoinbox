import email
import email.policy
import imaplib
import logging
from datetime import timedelta, timezone as dt_timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

from django.db.models import F
from django.utils.html import strip_tags
from django.utils.timezone import now

logger = logging.getLogger(__name__)

_BOUNCE_FROM   = ('mailer-daemon', 'postmaster')
_BOUNCE_SUBJ   = ('delivery status notification', 'undelivered mail', 'mail delivery failed',
                   'delivery failure', 'returned mail', 'failure notice')
_OOF_HEADERS   = ('auto-submitted',)
_COMPLAINT_HDR = ('x-report-abuse-type', 'x-abuse-report', 'x-arf')

_FIRST_SYNC_BACKFILL_DAYS = 30

# Static fallback if a provider's Sent folder can't be discovered via IMAP LIST
# (see _discover_sent_folder) — used only when the live \Sent flag scan fails.
_SENT_FOLDER_FALLBACK = {'google': '[Gmail]/Sent Mail', 'microsoft': 'Sent Items'}


def _discover_sent_folder(imap, account):
    """Find this account's Sent folder, caching the result on
    account.sent_folder so it isn't rediscovered on every sync.

    Uses a plain IMAP LIST and scans for the \\Sent mailbox-attribute flag —
    both Gmail and Office365/Exchange annotate it on a standard (non-extended)
    LIST response. Deliberately not RFC 6154 LIST (SPECIAL-USE): Python's
    imaplib has no public API for IMAP4rev1 extension parameters, only
    fragile, undocumented internals.
    """
    if account.sent_folder:
        return account.sent_folder
    folder = None
    try:
        typ, data = imap.list()
        for line in (data or []):
            text = line.decode(errors='replace') if isinstance(line, bytes) else str(line)
            if '\\Sent' in text:
                folder = text.rsplit('"', 2)[-2] if '"' in text else text.split()[-1].strip('"')
                break
    except Exception:
        pass
    if not folder:
        folder = _SENT_FOLDER_FALLBACK.get(account.provider, '')
    if folder:
        from Email_validate_app.models import SOEmailAccount
        SOEmailAccount.objects.filter(id=account.id).update(sent_folder=folder)
    return folder


def _record_once(cc, event_type, counter_field, metadata=None, ref_ids=None):
    """Record an SOEvent for a campaign contact at most once.

    The inbox is re-scanned every 15 minutes with day-granularity IMAP SEARCH,
    so the same reply/bounce is seen repeatedly. Without this guard the counters
    inflate on every pass (the previous behaviour: one reply sitting in the inbox
    for a week added ~672 to total_replied).
    """
    from Email_validate_app.models import SOCampaign, SOEvent

    exists = SOEvent.objects.filter(
        campaign_id=cc.campaign_id, email=cc.email, event_type=event_type,
    ).exists()
    if exists:
        return False
    # V3.2 step attribution — cc.current_step is always "the step still owed"
    # (see SOCampaignContact's own docstring), and _record_success always
    # writes current_step and message_id together, so cc.message_id (whatever
    # this reply/bounce/complaint was matched against, directly or via the
    # most-recent-send fallback in sync_account_inbox) always corresponds to
    # step current_step - 1, the step most recently sent to this contact.
    # Not a new lookup — cc is already resolved by the time this runs, this
    # just reads state the caller already has correctly. Guarded at 0 for a
    # row somehow reached with nothing ever sent, which should not happen
    # given every caller here already requires sent_at.
    step_order = cc.current_step - 1 if cc.current_step > 0 else None
    # V3.7 — 'replied' ONLY: refine step_order with a precise lookup against
    # this contact's own per-step 'sent' SOEvent history (written by
    # so_drip.py::_record_success, one row per step with its own message_id
    # and step_order) whenever the reply's own In-Reply-To/References
    # headers (ref_ids, collected by the caller) actually reference one of
    # those message-ids — this is what lets a LATE reply (arriving after
    # later steps have already been sent, so cc.current_step - 1 would
    # otherwise point at the wrong, more-recent step) still resolve to the
    # step it was actually replying to. Picks the LATEST (highest
    # step_order) referenced step when a thread spans several — the
    # prospect is almost always replying to the newest message they saw,
    # not an older one further back in the same thread. Falls back to the
    # heuristic above whenever no precise match is possible (no ref_ids, or
    # none of them match a known 'sent' event — e.g. the from-address-only
    # fallback path has no headers to match against at all). Deliberately
    # scoped to event_type == 'replied' only — bounced/complained keep the
    # exact same heuristic they always have, unchanged.
    if event_type == 'replied' and ref_ids:
        precise_step_order = (
            SOEvent.objects.filter(
                campaign_id=cc.campaign_id, email=cc.email, event_type='sent',
                message_id__in=ref_ids, step_order__isnull=False,
            )
            .order_by('-step_order')
            .values_list('step_order', flat=True)
            .first()
        )
        if precise_step_order is not None:
            step_order = precise_step_order
    SOEvent.objects.create(
        campaign=cc.campaign, prospect=cc.prospect,
        # The outbound campaign send this reply/bounce/complaint is about —
        # cc.account/cc.message_id, not the account whose inbox happened to
        # be doing the IMAP sync (usually the same account in this system,
        # but not guaranteed, and not what "sender account" means here).
        account_id=cc.account_id, message_id=cc.message_id,
        email=cc.email, event_type=event_type, metadata=metadata or {},
        step_order=step_order,
    )
    SOCampaign.objects.filter(id=cc.campaign_id).update(
        **{counter_field: F(counter_field) + 1}
    )
    return True


def _extract_email_address(from_header_raw: str) -> str:
    _, addr = parseaddr(from_header_raw or '')
    return addr.strip().lower() if addr else ''


def _parse_message_date(msg):
    raw = msg.get('Date')
    if not raw:
        return now()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except Exception:
        return now()


def _extract_body(msg_full):
    """(html, text) from a full RFC822 message parsed with policy=email.policy.default,
    which is what makes .get_body() available."""
    html, text = '', ''
    try:
        part = msg_full.get_body(preferencelist=('html',))
        if part is not None:
            html = part.get_content()
    except Exception:
        pass
    try:
        part = msg_full.get_body(preferencelist=('plain',))
        if part is not None:
            text = part.get_content()
    except Exception:
        pass
    if not text and html:
        text = strip_tags(html)
    return html, text


def _has_attachment(msg_full):
    try:
        for part in msg_full.iter_attachments():
            return True
    except Exception:
        pass
    return False


def _record_conversation_reply(cc, subject, own_msg_id, in_reply_to, from_addr,
                                account, sent_at, html, text, has_attachment, is_oof):
    """Persist an inbound reply into the Inbox's conversation/message store.
    Idempotent on (account, own_msg_id) — a re-scan of the same IMAP window
    must not create a second row for the same physical message."""
    from Email_validate_app.services.so_inbox import cc_thread_key, upsert_conversation_message

    upsert_conversation_message(
        thread_key=cc_thread_key(cc.id), account=account, direction='inbound',
        subject=subject, body_html=html, body_text=text,
        from_email=from_addr, to_email=account.email,
        message_id=own_msg_id, in_reply_to=in_reply_to,
        has_attachments=has_attachment, sent_at=sent_at,
        campaign_contact=cc, campaign=cc.campaign, prospect=cc.prospect,
        counterpart_email=cc.email,
        # Out-of-office auto-classifies (only if the user hasn't already
        # classified this conversation manually — never clobber a human's call).
        classification_if_new='out_of_office' if is_oof else '',
    )


def _record_general_message(imap, msg, num, account, direction):
    """Record inbound/outbound mail that doesn't match any SOCampaignContact —
    the general 'Others'/'Sent' path. Opportunistically links a known SOProspect
    by address so it can surface under Primary instead of Others even without
    an active campaign enrollment."""
    from Email_validate_app.models import SOProspect
    from Email_validate_app.services.so_inbox import account_thread_key, upsert_conversation_message

    try:
        _, raw = imap.fetch(num, '(RFC822)')
        raw_bytes = raw[0][1] if raw and raw[0] else b''
        msg_full = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        html, text = _extract_body(msg_full)
        has_attachment = _has_attachment(msg_full)
    except Exception:
        html, text, has_attachment = '', '', False

    own_msg_id      = _decode_header_value(msg.get('Message-ID', '')).strip()
    sent_at         = _parse_message_date(msg)
    subject_display = _decode_header_value(msg.get('Subject', ''))
    from_addr       = _extract_email_address(msg.get('From', ''))
    # Multi-recipient Sent messages: first To-address only — a deliberate
    # simplification, not a bug (this codebase doesn't model multi-party threads).
    to_addr         = _extract_email_address(_decode_header_value(msg.get('To', '')).split(',')[0])

    counterpart = to_addr if direction == 'outbound' else from_addr
    if not counterpart:
        return

    prospect = SOProspect.objects.filter(
        user_id=account.user_id, email__iexact=counterpart, deleted_at__isnull=True,
    ).first()

    upsert_conversation_message(
        thread_key=account_thread_key(account.id, counterpart), account=account, direction=direction,
        subject=subject_display, body_html=html, body_text=text,
        from_email=from_addr or account.email, to_email=to_addr or account.email,
        message_id=own_msg_id, has_attachments=has_attachment, sent_at=sent_at,
        campaign_contact=None, campaign=None, prospect=prospect,
        counterpart_email=counterpart,
    )


def _decode_header_value(raw) -> str:
    if not raw:
        return ''
    parts = decode_header(raw)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(chunk)
    return ''.join(result)


def sync_account_inbox(account):
    """
    Connect to the IMAP inbox of an SOEmailAccount and detect:
    - Replies   (In-Reply-To / References match a sent Message-ID)
    - Bounces   (MAILER-DAEMON / DSN subjects)
    - OOO       (Auto-Submitted: auto-replied)
    - Complaints (abuse-report headers)

    Updates account.last_imap_sync on success.
    """
    from Email_validate_app.models import (
        SOCampaignContact, SOProspect, SOEvent, SOCampaign,
    )
    from Email_validate_app.services.so_smtp import decrypt_password

    try:
        plain_pwd = decrypt_password(account)
    except Exception as exc:
        logger.error('so_imap: cannot decrypt password for account %s: %s', account.id, exc)
        return

    # A brand-new account gets a much wider first-sync backfill window so its
    # Others/Sent history isn't empty on day one; every subsequent sync keeps
    # using the real incremental last_imap_sync exactly as before.
    is_first_sync = account.last_imap_sync is None
    fallback_hours = _FIRST_SYNC_BACKFILL_DAYS * 24 if is_first_sync else 24
    since = account.last_imap_sync or (now() - timedelta(hours=fallback_hours))
    since_str = since.strftime('%d-%b-%Y')

    try:
        if account.imap_ssl:
            imap = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        else:
            imap = imaplib.IMAP4(account.imap_host, account.imap_port)
        imap.login(account.username, plain_pwd)
    except Exception as exc:
        logger.error('so_imap: IMAP connect/login failed for account %s: %s', account.id, exc)
        return

    def _handle_reply_candidates(msg, num, cc_qs, auto_sub, in_reply_to, ref_ids=None):
        """Shared by the ref-matched path and the no-headers-at-all fallback
        path — fetches the full body ONCE (header-only fetch stays cheap for
        every message; this second targeted fetch only runs for confirmed
        reply candidates) and records the reply into both the analytics
        counters (SOEvent, unchanged) and the Inbox's content store.

        `ref_ids` (V3.7) is the full In-Reply-To/References message-id set
        the caller already computed for thread matching — passed through
        unchanged to _record_once for precise 'replied' step attribution
        only; None/empty from the no-headers-at-all fallback path, where no
        precise attribution is possible and the existing heuristic applies.
        """
        if not cc_qs:
            # Not a campaign reply — still real mail in this mailbox. Record it
            # under the general (non-campaign) conversation path instead of
            # dropping it (the pre-Unibox behavior).
            _record_general_message(imap, msg, num, account, direction='inbound')
            return
        from Email_validate_app.services.so_drip import stop as _stop_sequence

        is_oof = 'auto-replied' in auto_sub
        try:
            _, raw = imap.fetch(num, '(RFC822)')
            raw_bytes = raw[0][1] if raw and raw[0] else b''
            msg_full = email.message_from_bytes(raw_bytes, policy=email.policy.default)
            html, text = _extract_body(msg_full)
            has_attachment = _has_attachment(msg_full)
        except Exception:
            html, text, has_attachment = '', '', False
        own_msg_id      = _decode_header_value(msg.get('Message-ID', '')).strip()
        sent_at         = _parse_message_date(msg)
        subject_display = _decode_header_value(msg.get('Subject', ''))
        from_addr       = _extract_email_address(msg.get('From', ''))

        for cc in cc_qs:
            _record_once(cc, 'replied', 'total_replied', {'oof': is_oof}, ref_ids=ref_ids)
            # An out-of-office is not a genuine reply — don't stop a
            # sequence over an auto-responder, and never let it feed
            # reply-based branching either (see services/so_subsequence.py
            # ::_eval_replied, which independently excludes metadata['oof']
            # rows as a second, structural guard against the same thing).
            if not is_oof:
                # V3.7 — a campaign with an ACTIVE 'replied' branching
                # condition gets to decide what a reply means instead of
                # the sequence being unconditionally stopped: leave the
                # contact exactly as-is (still 'active', unchanged
                # next_action_at) so the existing, unmodified condition
                # dispatcher (tasks/so_subsequence.py, already polls every
                # 15 minutes with no next_action_at gate) picks it up on
                # its own next tick via eligible_condition_branch/
                # _eval_replied — no new dispatch mechanism, no change to
                # branch_via_condition or the CAS logic. A campaign with NO
                # active 'replied' condition takes exactly the same
                # stop() path it always has — zero behavior change.
                #
                # V3.9 — also requires cc.active_subsequence_id is None: a
                # main-sequence 'replied' condition can never apply to a
                # contact already on a subsequence track (
                # eligible_condition_branch's own active_subsequence_id
                # gate rejects them unconditionally), so skipping stop()
                # for one bought nothing but an un-stopped contact who
                # genuinely replied — see the V3.6-V3.8 audit's Medium
                # finding. A subsequence contact now always stops on a
                # genuine reply, exactly as every campaign already behaved
                # before V3.7 existed.
                has_reply_condition = (
                    cc.active_subsequence_id is None
                    and cc.campaign.conditions.filter(trigger_type='replied', is_active=True).exists()
                )
                if not has_reply_condition:
                    _stop_sequence(cc, 'replied')
            _record_conversation_reply(
                cc, subject_display, own_msg_id, in_reply_to, from_addr,
                account, sent_at, html, text, has_attachment, is_oof,
            )

    try:
        imap.select('INBOX', readonly=True)
        _, data = imap.search(None, f'(SINCE "{since_str}")')
        msg_nums = data[0].split() if data[0] else []

        for num in msg_nums:
            try:
                _, raw = imap.fetch(num, '(RFC822.HEADER)')
                header_data = raw[0][1] if raw and raw[0] else b''
                msg = email.message_from_bytes(header_data)
            except Exception:
                continue

            from_hdr    = _decode_header_value(msg.get('From', '')).lower()
            subject_hdr = _decode_header_value(msg.get('Subject', '')).lower()
            in_reply_to = _decode_header_value(msg.get('In-Reply-To', '')).strip()
            references  = _decode_header_value(msg.get('References', '')).strip()
            auto_sub    = _decode_header_value(msg.get('Auto-Submitted', '')).lower()

            try:
                # Reply detection — thread match first; if the References/In-Reply-To
                # header is present but doesn't match anything we sent (broken
                # threading in the wild), fall back to matching by From-address.
                if in_reply_to or references:
                    ref_ids = set(filter(None, [in_reply_to] + references.split()))
                    cc_qs = list(
                        SOCampaignContact.objects.filter(message_id__in=ref_ids)
                        .select_related('prospect', 'campaign')
                    )
                    if not cc_qs:
                        from_addr = _extract_email_address(msg.get('From', ''))
                        if from_addr:
                            fallback_cc = SOCampaignContact.objects.filter(
                                email__iexact=from_addr, campaign__user_id=account.user_id, sent_at__isnull=False,
                            ).select_related('prospect', 'campaign').order_by('-sent_at').first()
                            cc_qs = [fallback_cc] if fallback_cc else []
                    _handle_reply_candidates(msg, num, cc_qs, auto_sub, in_reply_to, ref_ids=ref_ids)

                # Bounce detection (only if not already handled as reply)
                elif (any(b in from_hdr for b in _BOUNCE_FROM) or
                      any(subject_hdr.startswith(s) for s in _BOUNCE_SUBJ)):
                    from Email_validate_app.services.so_drip import stop_all_for_email

                    # Gmail and most MTAs name the dead address in X-Failed-Recipients;
                    # fall back to the original Message-ID when the DSN quotes it.
                    failed = _decode_header_value(msg.get('X-Failed-Recipients', '')).strip().lower()
                    bounced_ccs = []
                    if failed:
                        addrs = [a.strip() for a in failed.split(',') if a.strip()]
                        # One lookup PER address, always scoped to this account's
                        # own user — a combined filter+slice across all addresses
                        # (the previous approach) could both leak a match across
                        # tenants (no user scoping at all) and mismatch which
                        # address got which contact when a DSN lists several.
                        for addr in addrs:
                            cc_for_addr = (
                                SOCampaignContact.objects
                                .filter(email__iexact=addr, sent_at__isnull=False,
                                        campaign__user_id=account.user_id)
                                .select_related('prospect', 'campaign')
                                .order_by('-sent_at')
                                .first()
                            )
                            if cc_for_addr:
                                bounced_ccs.append(cc_for_addr)
                    if not bounced_ccs and (in_reply_to or references):
                        ref_ids = set(filter(None, [in_reply_to] + references.split()))
                        bounced_ccs = list(
                            SOCampaignContact.objects.filter(
                                message_id__in=ref_ids, campaign__user_id=account.user_id,
                            ).select_related('prospect', 'campaign')
                        )
                    if bounced_ccs:
                        for cc in bounced_ccs:
                            _record_once(cc, 'bounced', 'total_bounced', {'subject': subject_hdr[:200]})
                            # Stops this contact AND every other in-flight
                            # contact for the same address across this same
                            # user's other currently-running campaigns.
                            stop_all_for_email(account.user_id, cc.email, 'bounced')
                    else:
                        logger.info('so_imap: bounce for account %s could not be matched: %s',
                                    account.id, subject_hdr[:120])

                # No threading headers at all, and not bounce-looking — some clients
                # drop In-Reply-To/References entirely. Last resort: match by
                # From-address against a contact we've actually sent to.
                else:
                    from_addr = _extract_email_address(msg.get('From', ''))
                    cc_qs = []
                    if from_addr:
                        fallback_cc = SOCampaignContact.objects.filter(
                            email__iexact=from_addr, campaign__user_id=account.user_id, sent_at__isnull=False,
                        ).select_related('prospect', 'campaign').order_by('-sent_at').first()
                        cc_qs = [fallback_cc] if fallback_cc else []
                    _handle_reply_candidates(msg, num, cc_qs, auto_sub, in_reply_to)

                # Complaint detection — V1 treats a complaint exactly like a
                # bounce: stop this contact and every other in-flight contact
                # for the same address across this user's other running
                # campaigns, and keep future enrollment excluded (see the
                # suppressed_emails check in so_send_campaign_task).
                for h in _COMPLAINT_HDR:
                    if msg.get(h):
                        from Email_validate_app.services.so_drip import stop_all_for_email
                        ref_ids = set(filter(None, [in_reply_to] + references.split()))
                        for cc in SOCampaignContact.objects.filter(
                            message_id__in=ref_ids, campaign__user_id=account.user_id,
                        ).select_related('prospect', 'campaign'):
                            _record_once(cc, 'complained', 'total_complained', {'header': h})
                            stop_all_for_email(account.user_id, cc.email, 'complained')
                        logger.info('so_imap: complaint header "%s" detected for account %s', h, account.id)
                        break
            except Exception:
                # One odd message (unusual encoding, malformed MIME, a transient
                # DB error) must not abort the rest of this sync batch — the
                # general/'Others' mail path now processes real-world inbound
                # mail far more broadly than the old campaign-reply-only path.
                logger.exception('so_imap: failed to process message %s for account %s', num, account.id)
                continue

        # Sent folder — captures outbound mail this account sent that this app
        # didn't already write directly (e.g. sent from the provider's own web
        # UI), and lets the general/Sent-folder dedup pre-check in
        # upsert_conversation_message skip re-recording mail this app DID
        # already write (sequence sends, manual replies).
        sent_folder = _discover_sent_folder(imap, account)
        if sent_folder:
            try:
                # imaplib does not auto-quote mailbox names — an unquoted name
                # containing a space (e.g. "[Gmail]/Sent Mail") makes the server
                # reject SELECT/EXAMINE with "Could not parse command".
                imap.select('"' + sent_folder.replace('"', '\\"') + '"', readonly=True)
                _, sent_data = imap.search(None, f'(SINCE "{since_str}")')
                sent_nums = sent_data[0].split() if sent_data[0] else []
            except Exception as exc:
                logger.warning('so_imap: cannot read Sent folder "%s" for account %s: %s',
                                sent_folder, account.id, exc)
                sent_nums = []
            for num in sent_nums:
                try:
                    _, raw = imap.fetch(num, '(RFC822.HEADER)')
                    header_data = raw[0][1] if raw and raw[0] else b''
                    sent_msg = email.message_from_bytes(header_data)
                    _record_general_message(imap, sent_msg, num, account, direction='outbound')
                except Exception:
                    logger.exception('so_imap: failed to process Sent message %s for account %s', num, account.id)
                    continue

    finally:
        try:
            imap.logout()
        except Exception:
            pass
        account.last_imap_sync = now()
        account.save(update_fields=['last_imap_sync'])
