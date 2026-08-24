"""
so_subsequence.py
------------------
Branch evaluation for Sales Outreach subsequences — deciding WHICH track a
contact should be on. Sending the step a contact currently owes stays entirely
in services/so_drip.py; this module only ever changes cc.active_subsequence.

A campaign's subsequences are chained by `order`: a contact on track T (the
main sequence, or the subsequence at order=k) becomes eligible for the next
subsequence (order=0 from the main sequence, or order=k+1) once that
subsequence's trigger_days have passed since the contact's last successful
send on track T, with no reply/bounce/unsubscribe in between.
"""

import logging

from django.utils.timezone import now

logger = logging.getLogger(__name__)


def eligible_next_subsequence(cc):
    """Pure — no writes. Returns the SOSubsequence `cc` should branch into
    next, or None if no chained subsequence exists yet or the trigger hasn't
    fired. Never a reply/bounce/unsubscribe (status must still be 'active' or
    'completed' — stop() already moved anything else to 'stopped')."""
    if cc.status not in ('active', 'completed'):
        return None
    if not cc.sent_at:
        return None

    current_order = cc.active_subsequence.order if cc.active_subsequence_id else -1
    next_sub = (
        cc.campaign.subsequences
        .filter(is_active=True, order__gt=current_order)
        .order_by('order')
        .first()
    )
    if next_sub is None:
        return None

    from datetime import timedelta
    cutoff = cc.sent_at + timedelta(days=next_sub.trigger_days)
    if now() < cutoff:
        return None
    return next_sub


def branch_contact(cc, subsequence):
    """Atomically move `cc` onto `subsequence`'s steps, starting at step 0.

    Conditional UPDATE gated on the contact still being on the exact track it
    was evaluated on (active_subsequence_id unchanged) and still eligible
    (status active/completed) — the same race-prevention shape as
    so_drip.stop()/_reserve_quota_slot, so a contact can't be double-branched
    or branched past a track it hasn't actually reached.

    Also re-opens the campaign if so_dispatch_due_sequence_steps already
    finalized it to 'sent' (which happens the moment every contact finishes
    the main sequence — often true right before a no-reply trigger fires).
    That dispatcher's own due-contact query is scoped to campaign__status=
    'sending', so without this, a contact branched into a fresh active
    subsequence would never be picked up again. Re-opening here — rather than
    widening that query — keeps so_dispatch_due_sequence_steps itself
    untouched; its own finalization check re-closes the campaign once the
    subsequence chain is truly exhausted, same as it already does today.
    """
    from Email_validate_app.models import SOCampaign, SOCampaignContact

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status__in=('active', 'completed'), active_subsequence_id=cc.active_subsequence_id,
    ).update(active_subsequence=subsequence, current_step=0, next_action_at=now(), status='active')
    if updated:
        SOCampaign.objects.filter(id=cc.campaign_id, status='sent').update(status='sending')
        logger.info(
            'so_subsequence: campaign contact %s (%s) branched into subsequence %s (%s)',
            cc.id, cc.email, subsequence.id, subsequence.name,
        )
    return bool(updated)
