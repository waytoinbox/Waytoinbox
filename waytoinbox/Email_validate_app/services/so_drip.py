"""
so_drip.py
----------
Per-recipient execution for a Sales Outreach multi-step sequence.

This module owns exactly one thing: sending the step a given SOCampaignContact
currently owes, then advancing (or stopping) its sequence state. It calls the
existing SMTP helpers in services/so_smtp.py unchanged — nothing about how a
message is built or delivered is touched here, only which body goes out and what
happens to the contact row afterward.

The Celery side (tasks/so_send_campaign.py::so_dispatch_due_sequence_steps) is
responsible for finding due contacts and claiming them atomically before calling
send_next_step(); this module assumes the caller already holds the claim
(status was flipped active -> sending by a conditional UPDATE).
"""

import hashlib
import logging
import smtplib
import uuid
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.db.models import F
from django.utils.timezone import now

logger = logging.getLogger(__name__)

MAX_ATTEMPTS  = 3
RETRY_DELAY   = timedelta(minutes=15)
WEEKDAY_ABBR  = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']  # Python .weekday() order


def pick_variant_label(campaign_id, email, step_id, variants):
    """Deterministic per-recipient, PER-STEP A/B pick over one step's active
    variants, weighted by their configured Weight (validated server-side to
    sum to exactly 100 across active variants — see
    views/so_sender.py::_validate_step_list — so these weights are already
    real percentages by the time this function ever sees them).

    Hash-based rather than random so re-running/retrying a send assigns the
    same recipient the same variant instead of re-rolling it — same
    technique as pick_weighted_account below, now also salted with the
    step's own stable id (not its order, which can change on reorder) so
    EVERY step is resolved independently: campaign_id + email + step_id
    together are the sole selection key, per step. Revisiting the same step
    later (e.g. after a branch) recomputes to the exact same variant, since
    nothing here depends on when it's called, only on these three inputs.

    Weight 0 means excluded, not "rare" — unlike the historical
    max(1, v.weight) floor this replaces, a variant with weight 0 is
    filtered out of the draw entirely, exactly mirroring
    pick_weighted_account's own "0 means configured but disabled" rule
    below (that function's docstring used to call this an intentional
    difference between the two; it no longer is one).
    """
    eligible = [v for v in variants if v.is_active and v.weight > 0]
    if not eligible:
        # Defensive fallback for legacy/malformed data only — write-path
        # validation now requires at least one active variant with weight
        # > 0 (summing to exactly 100), so this should not be reachable for
        # any campaign saved under the current validation rules.
        active = [v for v in variants if v.is_active]
        pool = active or list(variants)
        return pool[0].label if pool else 'A'
    if len(eligible) == 1:
        return eligible[0].label
    h = int(hashlib.sha256(f'{campaign_id}:{email}:step{step_id}'.encode()).hexdigest()[:8], 16)
    weights = [v.weight for v in eligible]
    total   = sum(weights)
    target  = h % total
    acc = 0
    for v, w in zip(eligible, weights):
        acc += w
        if target < acc:
            return v.label
    return eligible[-1].label


def pick_weighted_account(campaign_id, email, rotations):
    """Deterministic per-recipient weighted pick over a campaign's selected
    sender accounts (V2.4.8). Same hash-based technique as pick_variant_label
    above, for the same reason — re-running enrollment (e.g. a retried task)
    assigns the same recipient the same account instead of re-rolling it,
    and sticky assignment (SOCampaignContact.account) then keeps every
    subsequent step on that same account. The hash input includes a distinct
    ':account' suffix so which account a contact gets is decorrelated from
    which A/B variant it gets — two independent draws, not the same one
    reused twice.

    A weight of 0 is never selected here — an explicitly zero-weighted
    account must never be picked (that's the whole point of allowing 0:
    "configured but effectively disabled"), same rule pick_variant_label
    above now applies to variants too. `rotations` must already be
    pre-filtered to only
    connected, non-deleted accounts by the caller — this function has no
    opinion on account eligibility, only on weighting among what it's given.
    Returns None if nothing is eligible (every rotation has weight 0, or the
    list is empty) — the caller already knows how to handle "no account"
    (see send_next_step's own no-valid-account bounded retry).
    """
    eligible = [r for r in rotations if r.weight > 0]
    if not eligible:
        return None
    if len(eligible) == 1:
        return eligible[0].account
    h = int(hashlib.sha256(f'{campaign_id}:{email}:account'.encode()).hexdigest()[:8], 16)
    weights = [r.weight for r in eligible]
    total   = sum(weights)
    target  = h % total
    acc = 0
    for r, w in zip(eligible, weights):
        acc += w
        if target < acc:
            return r.account
    return eligible[-1].account


def stop(cc, reason):
    """Halt a contact's sequence. Idempotent — a conditional UPDATE, not a
    read-modify-write, so concurrent stop signals (reply + manual cancel, etc.)
    can't race each other."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status__in=('active', 'sending'),
    ).update(status='stopped', error=f'stopped: {reason}', next_action_at=None)
    if updated:
        logger.info('so_drip: campaign contact %s (%s) stopped — %s', cc.id, cc.email, reason)
    return bool(updated)


def resume(cc):
    """Mirror of stop() — manually resume a paused contact's sequence. The
    dispatcher picks it back up on its next tick, respecting whatever
    send-window/quota checks already apply; no special-casing needed here."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status='stopped',
    ).update(status='active', next_action_at=now(), error='')
    if updated:
        logger.info('so_drip: campaign contact %s (%s) resumed', cc.id, cc.email)
    return bool(updated)


def stop_all_for_email(user_id, email, reason):
    """Stop every in-flight SOCampaignContact for this email across ALL of
    this user's campaigns — not just the one campaign that detected the
    bounce/complaint. A bounce or complaint is evidence about the address
    itself, not about one particular campaign, so no other currently-running
    campaign of this user's should keep mailing it either. Scoped strictly to
    `user_id` — must never affect a different tenant's campaign contacts."""
    from Email_validate_app.models import SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        campaign__user_id=user_id, email=email, status__in=('active', 'sending'),
    ).update(status='stopped', error=f'stopped: {reason}', next_action_at=None)
    if updated:
        logger.info('so_drip: stopped %s in-flight contact(s) for %s (user %s) — %s',
                    updated, email, user_id, reason)
    return updated


def _resolve_step_and_variant(campaign, cc):
    # active_subsequence_id set = the contact branched off the main sequence
    # (services/so_subsequence.py::branch_contact) — current_step then indexes
    # into that subsequence's own steps instead of campaign.steps.
    step_holder = cc.active_subsequence if cc.active_subsequence_id else campaign
    step = step_holder.steps.filter(order=cc.current_step).prefetch_related('variants').first()
    if step is None:
        return None, None
    variants = list(step.variants.all())

    # V4.x variation-weight fix — cc.variant_label doubles as a permanent,
    # enrollment-time sentinel of WHICH generation of selection logic this
    # contact belongs to, decided once and never overwritten afterward (not
    # by branching, not by retries, not by any later send):
    #   - non-empty (every contact enrolled before this fix, and every
    #     legacy row already has a real label) -> the ORIGINAL sticky
    #     lookup, byte-for-byte unchanged, so an already-enrolled contact's
    #     remaining steps are never retroactively reassigned to a different
    #     variant than what earlier steps in the SAME sequence already sent.
    #   - empty (tasks/so_send_campaign.py now enrolls new contacts with
    #     variant_label='' instead of pre-picking a step-0 label) -> a
    #     fresh, independent, per-step deterministic weighted pick via
    #     pick_variant_label, keyed on this exact step's own id — so
    #     different steps can and do resolve to different variants for the
    #     same recipient, each honoring THAT step's own configured weights.
    if cc.variant_label:
        variant = next((v for v in variants if v.is_active and v.label == cc.variant_label), None)
    else:
        label = pick_variant_label(campaign.id, cc.email, step.id, variants)
        variant = next((v for v in variants if v.is_active and v.label == label), None)

    if variant is None:
        active = [v for v in variants if v.is_active]
        variant = (active or variants or [None])[0]
    return step, variant


def _get_contact_account(cc):
    """Resolve the sender account for this contact.

    Sticky: returns cc.account if already assigned — every step of a recipient's
    sequence sends from the same account (see models.SOCampaignContact.account).
    Self-heals legacy rows (created before this field existed) by round-robin
    picking from cc.campaign.account_rotations — never the user's full account
    pool, only the accounts actually selected for THIS campaign — and persisting
    the choice so it stays sticky from here on. Returns None if the campaign has
    no live rotation accounts at all.
    """
    from Email_validate_app.models import SOCampaignContact

    if cc.account_id:
        return cc.account

    rotations = list(
        cc.campaign.account_rotations
        .filter(account__deleted_at__isnull=True, account__status='connected')
        .select_related('account')
        .order_by('order')
    )
    if not rotations:
        return None

    # Weighted pick (V2.4.8), same deterministic technique used at real
    # enrollment (tasks/so_send_campaign.py) — this is only a self-heal path
    # for legacy rows that somehow reached send-time with no account
    # assigned yet, so it must use the exact same selection logic rather
    # than a second, different mechanism.
    account = pick_weighted_account(cc.campaign_id, cc.email, rotations)
    if account is None:
        return None
    SOCampaignContact.objects.filter(id=cc.id, account__isnull=True).update(account=account)
    cc.account = account
    return account


def _next_utc_midnight():
    """The next UTC-day boundary — the moment SOEmailAccountDailyUsage resets."""
    today_utc = now().date()
    return datetime.combine(today_utc + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)


def _reserve_quota_slot(account):
    """Atomically claim one of `account`'s remaining sends for today, capped
    at min(account.daily_limit, 7) while the account's owning user has an
    active free trial -- a trial only ever narrows the cap, never widens it
    past the account's own configured capacity.

    Same conditional-UPDATE claim pattern already used to claim due contacts
    (so_dispatch_due_sequence_steps) — the WHERE clause is evaluated against the
    row's current committed value under MySQL/InnoDB row locking, so two
    concurrent callers for the same account cannot both succeed.

    Returns (claimed, effective_limit) — effective_limit lets the caller log
    what cap was actually enforced, which matters when a trial is deferring
    sends well below the account's own daily_limit.
    """
    from Email_validate_app.models import SOEmailAccountDailyUsage
    from Email_validate_app.services.trial_manager import sales_outreach_daily_send_cap

    today = now().date()
    trial_cap = sales_outreach_daily_send_cap(account.user_id)
    effective_limit = min(account.daily_limit, trial_cap) if trial_cap is not None else account.daily_limit

    SOEmailAccountDailyUsage.objects.get_or_create(account=account, date=today, defaults={'sent_count': 0})
    updated = SOEmailAccountDailyUsage.objects.filter(
        account=account, date=today, sent_count__lt=effective_limit,
    ).update(sent_count=F('sent_count') + 1)
    return bool(updated), effective_limit


def _release_quota_slot(account):
    """Give back a slot reserved by _reserve_quota_slot — called only when the
    send attempt that reserved it then failed. daily_limit counts successful
    sends only, so a failed attempt must not permanently consume a slot."""
    from Email_validate_app.models import SOEmailAccountDailyUsage

    today = now().date()
    SOEmailAccountDailyUsage.objects.filter(
        account=account, date=today, sent_count__gt=0,
    ).update(sent_count=F('sent_count') - 1)


def _campaign_tz(campaign):
    from zoneinfo import ZoneInfo, available_timezones

    tz_name = campaign.schedule_timezone
    if tz_name not in available_timezones():
        tz_name = 'UTC'
    return ZoneInfo(tz_name)


def _local_now(campaign):
    return now().astimezone(_campaign_tz(campaign))


def _allowed_weekdays(campaign):
    """Parse send_weekdays into a set of abbreviations, e.g. {'mon','wed'}.

    ''.split(',') yields [''] rather than [] — filtering blank entries is what
    makes the "empty/garbage value -> treat as unrestricted" fallback actually
    reachable in _next_window_start.
    """
    days = {d for d in (campaign.send_weekdays or '').split(',') if d}
    return days or set(WEEKDAY_ABBR)


def _in_send_window(campaign):
    """Is right now, in this campaign's timezone, an allowed day+time to send?

    Sending Days & Hours is a standing constraint on every send this campaign
    makes for its whole life — distinct from schedule_at, which only decides
    when step 1 launches. Defaults (all 7 days, 00:00-23:59:59) are the
    "unrestricted" sentinel, so an untouched campaign is never gated by this.
    """
    local = _local_now(campaign)
    allowed = _allowed_weekdays(campaign)
    if WEEKDAY_ABBR[local.weekday()] not in allowed:
        return False
    t = local.time()
    return campaign.send_hour_start <= t <= campaign.send_hour_end


def _next_window_start(campaign):
    """The next moment (UTC) this campaign is allowed to send, given it isn't
    allowed right now. Scans forward day by day (max 8, i.e. a full week plus
    one) for the next day in send_weekdays, at send_hour_start local time."""
    local = _local_now(campaign)
    allowed = _allowed_weekdays(campaign)

    for delta in range(8):
        d = local.date() + timedelta(days=delta)
        if WEEKDAY_ABBR[d.weekday()] not in allowed:
            continue
        candidate = datetime.combine(d, campaign.send_hour_start, tzinfo=local.tzinfo)
        if candidate > local:
            return candidate.astimezone(dt_timezone.utc)
    # Defensive fallback — unreachable with a non-empty `allowed`, since some
    # day within the next 7 must qualify.
    return now() + timedelta(hours=1)


def send_next_step(cc):
    """Send whatever step `cc` currently owes. Caller must already hold the claim
    (cc.status == 'sending'). Returns True on a successful send."""
    from Email_validate_app.models import SOProspect, SOCampaignContact, SOCampaign, SOEvent
    from Email_validate_app.services.so_smtp import inject_tracking, build_message, open_smtp

    campaign = cc.campaign

    # ── Eligibility re-check — state may have changed since this contact was
    # enrolled or since it was claimed. ──────────────────────────────────────
    if cc.prospect_id:
        still_subscribed = SOProspect.objects.filter(
            id=cc.prospect_id, status='subscribed', deleted_at__isnull=True,
        ).exists()
    else:
        still_subscribed = SOProspect.objects.filter(
            user_id=campaign.user_id, email=cc.email,
            status='subscribed', deleted_at__isnull=True,
        ).exists()
    if not still_subscribed:
        stop(cc, 'not_subscribed')
        return False

    # V3.11.1 — a bounce/complaint on a DIFFERENT campaign of this same user
    # only stops rows that were status__in=('active','sending') at that exact
    # moment (see stop_all_for_email) and never touches SOProspect — so a
    # contact already 'completed' elsewhere, with a still-pending clicked/
    # opened/replied condition, can later be branched back to 'active' by
    # that wholly unrelated condition and reach this point with
    # still_subscribed still True (bounce/complaint never flips it). This is
    # the final, universal choke point every send goes through regardless of
    # how the contact got here, so checking here — once — closes that gap
    # for every path (normal progression, subsequence branch, condition
    # branch) without touching any of them. Scoped identically to
    # stop_all_for_email/so_send_campaign_task's own enrollment-suppression
    # check (campaign__user_id, never cross-tenant). Reuses the exact
    # 'bounced'/'complained' reason strings already used at detection time —
    # no new vocabulary. Unsubscribe is untouched: it already works via
    # SOProspect.status, caught above by still_subscribed.
    suppressing_event_type = SOEvent.objects.filter(
        campaign__user_id=campaign.user_id, email=cc.email,
        event_type__in=('bounced', 'complained'),
    ).values_list('event_type', flat=True).first()
    if suppressing_event_type:
        stop(cc, suppressing_event_type)
        return False

    step, variant = _resolve_step_and_variant(campaign, cc)
    if step is None or variant is None:
        # No such step (e.g. deleted after enrollment) — nothing left to send.
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='completed', completed_at=now(), next_action_at=None,
        )
        logger.info('so_drip: campaign contact %s (%s) has no step %s — marking completed',
                    cc.id, cc.email, cc.current_step)
        return False

    if not _in_send_window(campaign):
        next_at = _next_window_start(campaign)
        logger.info(
            'so_drip: campaign %s contact %s outside sending window (%s %s-%s %s), deferred to %s',
            campaign.id, cc.id, campaign.send_weekdays, campaign.send_hour_start,
            campaign.send_hour_end, campaign.schedule_timezone, next_at.isoformat(),
        )
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', next_action_at=next_at,
        )
        return False

    account = _get_contact_account(cc)
    if not account or account.deleted_at:
        # Unlike quota exhaustion (a normal, recurring, self-resolving daily
        # condition — see below), "no valid sender account" only resolves if
        # someone fixes the campaign's account rotation. Bound the retry the
        # same way an SMTP send failure is bounded, so a permanently
        # misconfigured campaign eventually surfaces as failed instead of
        # polling forever every 15 minutes.
        attempts = cc.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
                status='failed', attempts=attempts,
                error='no valid connected sender account for this campaign',
                next_action_at=None,
            )
            SOCampaign.objects.filter(id=campaign.id).update(total_failed=F('total_failed') + 1)
            logger.error('so_drip: campaign %s contact %s failed permanently — no valid sender '
                        'account after %s attempts', campaign.id, cc.id, attempts)
        else:
            logger.error('so_drip: campaign %s has no valid sender account, contact %s attempt %s/%s',
                         campaign.id, cc.id, attempts, MAX_ATTEMPTS)
            SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
                status='active', attempts=attempts, next_action_at=now() + RETRY_DELAY,
            )
        return False

    claimed, effective_limit = _reserve_quota_slot(account)
    if not claimed:
        next_at = _next_utc_midnight()
        logger.info(
            'so_drip: campaign %s contact %s deferred — account %s (%s) at daily_limit=%s, retrying at %s',
            campaign.id, cc.id, account.id, account.email, effective_limit, next_at.isoformat(),
        )
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', next_action_at=next_at,
        )
        return False

    site_url = _site_url()
    # Set the instant server.sendmail() actually returns successfully — see
    # the except block below. Everything from that point on (persisting
    # tracked_links/open_pixel) must never be able to make an already-
    # delivered email look like a failed send: the recipient already has
    # it, so releasing the quota slot or scheduling a retry here would
    # either under-count today's real usage or send them the same step
    # again.
    sent_ok = False
    try:
        from django.conf import settings
        # The account-wide flag can only ever be narrowed by the per-campaign
        # setting, never re-widened — if tracking is off globally, no
        # campaign-level value can turn it back on.
        enable_tracking = settings.ENABLE_EMAIL_TRACKING and campaign.tracking_enabled
        personalized_html, tracked_links, open_pixel = inject_tracking(
            variant.html_body, cc, site_url, enable_tracking=enable_tracking,
            step_order=cc.current_step,
        )
        msg_id  = f'<{uuid.uuid4()}@{account.smtp_host}>'
        from_nm = campaign.from_name or account.display_name or account.email
        # The List-Unsubscribe header is a real link too — same rule as the
        # body: don't emit it pointing somewhere nothing is listening.
        unsub_url = f'{site_url}/so/unsubscribe/{cc.tracking_token}/' if enable_tracking else ''

        server = open_smtp(account)
        try:
            msg = build_message(from_nm, account.email, cc.email,
                                variant.subject, personalized_html, unsub_url, msg_id)
            refused = server.sendmail(account.email, cc.email, msg.as_bytes())
            if refused and cc.email in refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            try:
                server.quit()
            except Exception:
                pass

        # The email is now genuinely in the recipient's mailbox. Nothing
        # from here on may be treated as a send failure.
        sent_ok = True

        if tracked_links:
            from Email_validate_app.models import SOTrackedLink
            SOTrackedLink.objects.bulk_create(tracked_links, ignore_conflicts=True)
        # V3.6 — persisted only once the send has actually succeeded, same
        # timing as tracked_links above; open_pixel is None whenever
        # enable_tracking was False, so nothing is created for a
        # tracking-disabled send.
        if open_pixel is not None:
            open_pixel.save()

    except Exception as exc:
        if sent_ok:
            # Persisting the tracking rows failed AFTER a successful SMTP
            # delivery. Never route this through _record_failure — that
            # would release the quota slot the successful send legitimately
            # consumed and, worse, leave current_step unadvanced so the
            # dispatcher sends this same step again on the next pass,
            # duplicating a real email a prospect already received. Logged
            # loudly since this is otherwise invisible: tracking for this
            # one send may be incomplete, but the send itself proceeds
            # through the normal success path exactly as if nothing failed.
            logger.exception(
                'so_drip: campaign %s contact %s step %s — tracking '
                'persistence failed AFTER successful SMTP delivery; '
                'proceeding as a successful send (tracking data for this '
                'send may be incomplete): %s',
                campaign.id, cc.id, cc.current_step, exc,
            )
        else:
            logger.warning('so_drip: send failed for contact %s (%s) step %s: %s',
                           cc.id, cc.email, cc.current_step, exc)
            _release_quota_slot(account)   # reservation must not survive a failed send
            _record_failure(cc, exc)
            return False

    _record_success(cc, campaign, step, variant, msg_id, account, personalized_html)
    return True


def _site_url():
    from django.conf import settings
    return getattr(settings, 'SITE_URL', 'https://waytoinbox.com').rstrip('/')


def _record_conversation_send(cc, campaign, account, subject, html, msg_id, sent_at):
    """Mirror a successful sequence send into the Inbox's conversation/message
    store. SOEvent (below) stays the analytics/counter log; this is the content
    a human actually reads in the Inbox. Lazily creates the SOConversation on
    the contact's first send, same self-heal shape as _get_contact_account."""
    from django.utils.html import strip_tags
    from Email_validate_app.services.so_inbox import cc_thread_key, upsert_conversation_message

    upsert_conversation_message(
        thread_key=cc_thread_key(cc.id), account=account, direction='outbound',
        is_sequence_step=True, subject=subject, body_html=html, body_text=strip_tags(html).strip(),
        from_email=account.email, to_email=cc.email, message_id=msg_id, sent_at=sent_at,
        campaign_contact=cc, campaign=campaign,
        prospect=(cc.prospect if cc.prospect_id else None),
        counterpart_email=cc.email,
    )


def _record_success(cc, campaign, step, variant, msg_id, account, personalized_html):
    from Email_validate_app.models import SOCampaignContact, SOEvent, SOCampaign

    sent_at = now()
    step_holder = cc.active_subsequence if cc.active_subsequence_id else campaign
    next_step = step_holder.steps.filter(order=cc.current_step + 1).first()

    fields = {
        'status': 'active', 'sent_at': sent_at, 'message_id': msg_id,
        'attempts': 0, 'current_step': cc.current_step + 1,
    }
    if next_step is None:
        fields['status']         = 'completed'
        fields['completed_at']   = sent_at
        fields['next_action_at'] = None
    else:
        fields['next_action_at'] = sent_at + timedelta(
            days=next_step.wait_days, hours=next_step.wait_hours,
        )

    SOCampaignContact.objects.filter(id=cc.id, status='sending').update(**fields)
    # account/message_id give these rows real, typed attribution (not just an
    # untyped metadata key) — account is the exact sender account this send
    # used; message_id is the Message-ID generated for this send, joinable to
    # the SOMessage that _record_conversation_send writes just below.
    SOEvent.objects.create(
        campaign_id=campaign.id, prospect_id=cc.prospect_id, account_id=account.id,
        message_id=msg_id, email=cc.email,
        event_type='sent', metadata={'step': cc.current_step, 'account_id': account.id},
        step_order=cc.current_step,
    )
    SOEvent.objects.create(
        campaign_id=campaign.id, prospect_id=cc.prospect_id, account_id=account.id,
        message_id=msg_id, email=cc.email,
        event_type='delivered', metadata={'step': cc.current_step, 'account_id': account.id},
        step_order=cc.current_step,
    )
    SOCampaign.objects.filter(id=campaign.id).update(
        total_sent=F('total_sent') + 1, total_delivered=F('total_delivered') + 1,
    )
    _record_conversation_send(cc, campaign, account, variant.subject, personalized_html, msg_id, sent_at)
    logger.info(
        'so_drip: campaign %s contact %s — step %s sent, %s',
        campaign.id, cc.id, cc.current_step,
        'sequence completed' if fields['status'] == 'completed'
        else f"next action at {fields['next_action_at'].isoformat()}",
    )


def _record_failure(cc, exc):
    from Email_validate_app.models import SOCampaignContact, SOCampaign

    attempts = cc.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='failed', attempts=attempts, error=str(exc)[:2000], next_action_at=None,
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_failed=F('total_failed') + 1)
        logger.error('so_drip: campaign contact %s (%s) failed permanently after %s attempts: %s',
                     cc.id, cc.email, attempts, exc)
    else:
        SOCampaignContact.objects.filter(id=cc.id, status='sending').update(
            status='active', attempts=attempts, error=str(exc)[:2000],
            next_action_at=now() + RETRY_DELAY,
        )
        logger.warning('so_drip: campaign contact %s (%s) attempt %s/%s failed, retrying at +%s',
                       cc.id, cc.email, attempts, MAX_ATTEMPTS, RETRY_DELAY)
