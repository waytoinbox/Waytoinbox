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
    'completed' — stop() already moved anything else to 'stopped').

    V3.9 — that last sentence stopped being universally true the moment
    V3.7 added conditional reply-handling: services/so_imap.py now leaves
    cc.status='active' after a genuine reply whenever the campaign has an
    active main-sequence 'replied' condition, so it can be evaluated by
    eligible_condition_branch() instead of being unconditionally stopped.
    Without the check below, THIS function — which has no idea a reply
    happened, and only ever looked at status/elapsed time — could capture
    that contact into a "no reply" follow-up subsequence before the
    replied condition ever gets a chance to resolve; once captured,
    active_subsequence_id permanently blocks eligible_condition_branch()
    from ever reconsidering that contact (see its own first gate below),
    so the mistake could never self-correct.

    Deferring to eligible_condition_branch() itself (rather than
    reimplementing an equivalent "unresolved reply" query by hand) is
    deliberate: it already encodes every relevant rule exactly right —
    OOO exclusion (_eval_replied's metadata['oof'] check), main-sequence-
    only scope (its own active_subsequence_id gate, making this a no-op
    for a contact already on a subsequence), and "already resolved, don't
    re-block" (last_condition_id exclusion) — reusing it here can't drift
    out of sync with those rules the way a hand-rolled duplicate could.
    Scoped narrowly to a 'replied' resolution specifically: a clicked/
    opened condition being ready to fire is not a reason to defer
    subsequence capture — neither trigger type has an analogous stop()-
    skip side effect, so there was never any ambiguity for them to begin
    with, and deferring for them too would be an unrelated behavior
    change this fix has no reason to make.

    V4.0 — a SOConditionGroup can also contain a 'replied' member (mixed
    with 'clicked'/'opened'), and so_imap.py's conditional-stop rule
    already applies just the same to a contact with an eligible group
    (see so_imap.py — it checks for an active 'replied' condition on the
    campaign, which today's grouped 'replied' conditions still are, just
    combined). So the exact same capture-before-resolution bug applies to
    an eligible group with a replied member. Fixed the same way: defer to
    eligible_group_branch() itself rather than re-deriving "does this
    group contain an unresolved replied member" by hand — reusing the
    group's already-eligible member list can't drift out of sync with
    eligible_group_branch()'s own rules any more than the condition check
    above can.

    V4.1 (adversarial-audit finding, pre-existing since V3.9 — not
    introduced by V4.1's cascading, merely surfaced by it) — the two
    checks above only ever catch a replied condition/group that is STILL
    UNRESOLVED right now. The moment it actually resolves,
    branch_via_condition()/branch_via_group() correctly exclude it from
    firing again (last_condition_id / last_condition_group_id) — but that
    SAME exclusion also makes eligible_condition_branch()/
    eligible_group_branch() return nothing for it on any LATER call, so
    the two checks above see "no unresolved replied condition" and
    conclude there's nothing to defer for — even though the reason there's
    nothing to defer for is that a replied condition/group JUST correctly
    resolved for this exact contact. Without the two checks below, a
    contact who genuinely replied and was already correctly routed could,
    on the very next evaluation (another same-tick cascade hop, or an
    entirely separate later tick — reproduced with zero V4.1 code
    involved), be captured into a "no reply" follow-up subsequence anyway.
    Checked directly against last_condition_id/last_condition_group_id
    (already exactly "what last resolved for this contact") rather than
    querying SOEvent for a raw reply history, so this stays scoped to the
    same "was the most recent resolution a reply" question the two checks
    above already ask, just extended to cover the resolved case as well as
    the unresolved one — not a broader, independent suppression rule.
    """
    if cc.status not in ('active', 'completed'):
        return None
    if not cc.sent_at:
        return None

    condition, _ = eligible_condition_branch(cc)
    if condition is not None and condition.trigger_type == 'replied':
        return None

    group, _ = eligible_group_branch(cc)
    if group is not None and group.conditions.filter(trigger_type='replied').exists():
        return None

    if cc.last_condition_id:
        from Email_validate_app.models import SOSequenceCondition
        if SOSequenceCondition.objects.filter(id=cc.last_condition_id, trigger_type='replied').exists():
            return None

    if cc.last_condition_group_id:
        from Email_validate_app.models import SOConditionGroup
        if SOConditionGroup.objects.filter(
            id=cc.last_condition_group_id, conditions__trigger_type='replied',
        ).exists():
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


# ── V3.1/V3.3/V3.4 branching foundation ─────────────────────────────────────
#
# eligible_condition_branch()/branch_via_condition() extend this module with
# SOSequenceCondition support, following the exact same pure-check /
# atomic-UPDATE split as eligible_next_subsequence()/branch_contact() above.
#
# V3.4 adds the NO-branch half of 'clicked' (see _eval_clicked): if the
# source step's link was never clicked and wait_days have elapsed since that
# EXACT step's own 'sent' SOEvent timestamp (_source_step_sent_at — never
# cc.sent_at), the contact routes via condition.no_target_step instead.
# Resolution (YES or NO, either one) is final — the same last_condition_id
# exclusion described below for the YES case applies identically to NO, so a
# click arriving after a NO branch has already committed cannot resurrect
# the condition.
#
# Two trigger types exist, and they use genuinely different matching
# semantics against the contact's position — not a stylistic choice, a
# consequence of what each one actually means:
#
# 'no_event_after_days' (V3.1) PREEMPTIVELY reroutes a contact BEFORE
# source_step would otherwise be sent — current_step always means "the step
# still owed" (see SOCampaignContact's own field comment), so this condition
# can only ever be found while current_step == source_step.order. It
# deliberately reuses the SAME eligibility shape SOSubsequence's "no reply"
# trigger already uses (status must still be active/completed, enough time
# must have passed since cc.sent_at) rather than independently re-querying
# SOEvent for a reply's absence — "no reply has happened" is already exactly
# what status in (active, completed) means today, since so_drip.stop() is
# what moves a contact out of that pair the moment a reply/bounce/complaint/
# unsubscribe is recorded. This match is naturally self-terminating: once
# branched, current_step no longer equals source_step.order, so the same
# condition is never found again on a later tick.
#
# 'clicked' (V3.3) checks engagement with a step that has ALREADY been sent
# — the whole point (see the module's V3.3 worked example) is that a late
# click, arriving after later steps have already gone out, must still
# resolve to the step whose link was actually clicked. So it matches
# source_step.order < current_step ("this step is in the past"), which is
# NOT self-terminating the way the preemptive match is — current_step stays
# greater than source_step.order forever once it's passed. Re-evaluating the
# SAME already-fired condition on every later 15-minute tick would
# re-branch the contact indefinitely, so this path explicitly excludes any
# condition whose id already equals cc.last_condition_id (V3.1's own
# audit-trail field, repurposed here as the idempotency guard this matching
# shape needs — no new field required). The actual concurrency protection
# (two workers, same contact, same tick) still comes entirely from
# branch_via_condition()'s position-based compare-and-swap below, exactly
# as it already does for no_event_after_days; last_condition_id only
# prevents repeated SUCCESSFUL re-application across SEPARATE ticks, a
# sequential-time concern the exact-match trigger type never had.
#
# Both remain scoped to the main sequence only: a contact already on a
# subsequence track (active_subsequence_id set) has no main-sequence
# "current step" for source_step to mean anything against, so neither is
# evaluated — conditions do not chain into or out of subsequence tracks.

def _eval_no_event_after_days(cc, condition):
    if condition.yes_target_step_id is None:
        return None, None

    from datetime import timedelta
    cutoff = cc.sent_at + timedelta(days=condition.wait_days)
    if now() < cutoff:
        return None, None
    return condition, condition.yes_target_step


def _step_sent_at(cc, step):
    """The exact timestamp `step` was sent to this contact, or None if no
    such 'sent' SOEvent exists (e.g. legacy data predating V3.2's
    step_order). Deliberately NOT cc.sent_at — see _source_step_sent_at
    below, whose docstring this generalizes (V4.0 — a SOConditionGroup's
    shared source_step needs the identical lookup, so this takes the step
    directly rather than a condition)."""
    from Email_validate_app.models import SOEvent

    return (
        SOEvent.objects.filter(
            campaign_id=cc.campaign_id, email=cc.email, event_type='sent',
            step_order=step.order,
        )
        .order_by('created_at')
        .values_list('created_at', flat=True)
        .first()
    )


def _source_step_sent_at(cc, condition):
    """The exact timestamp source_step was sent to this contact, or None if
    no such 'sent' SOEvent exists (e.g. legacy data predating V3.2's
    step_order). Deliberately NOT cc.sent_at — by the time a 'clicked'
    condition is even reachable, source_step is already in the past, so
    cc.sent_at reflects whichever step was most recently sent, a LATER one.
    step_order (V3.2) is the only field that pins this down exactly."""
    return _step_sent_at(cc, condition.source_step)


def _condition_satisfied(cc, condition):
    """Pure YES-predicate for one condition's own trigger, independent of
    any NO/timeout branch — the exact existence/count check
    _eval_clicked/_eval_opened/_eval_replied already each perform for their
    own YES branch, factored out (V4.0) so a SOConditionGroup's AND/OR
    combination (see _eval_group) and every standalone evaluator share one
    source of truth rather than two copies that could drift apart. Never
    called for trigger_type='no_event_after_days' — it has no independent
    "is this true right now" predicate to combine into a group (see
    SOConditionGroup's own docstring), so it is never a valid group member
    and _eval_no_event_after_days does not use this helper."""
    from Email_validate_app.models import SOEvent

    if condition.trigger_type == 'replied':
        return SOEvent.objects.filter(
            campaign_id=cc.campaign_id, email=cc.email, event_type='replied',
            step_order=condition.source_step.order,
        ).exclude(metadata__oof=True).exists()

    qs = SOEvent.objects.filter(
        campaign_id=cc.campaign_id, email=cc.email, event_type=condition.trigger_type,
        step_order=condition.source_step.order,
    )
    threshold = condition.event_count_threshold
    if threshold is None or threshold <= 0:
        return qs.exists()
    return qs.count() >= threshold


def _eval_clicked(cc, condition):
    """trigger_type='clicked' — three conceptual states (V3.4), now with
    optional event_count_threshold (V3.5):

    WAITING_FOR_CLICK — source_step was sent (guaranteed by the caller's
        source_step__order__lt=cc.current_step filter) but neither the click
        requirement nor the wait_days timeout has resolved anything yet ->
        (None, None), re-checked on a later tick.
    RESOLVED (YES) — the click requirement is satisfied for source_step.order:
        event_count_threshold unset (None) or <= 0 preserves the exact V3.3/
        V3.4 fast path (SOEvent(...).exists()); a positive threshold instead
        requires SOEvent(...).count() >= threshold. Either way this is a
        direct, exact lookup against SOEvent.step_order (V3.2), which
        so_tracking.py::so_track_click already populates straight from the
        clicked SOTrackedLink's own step_order — no SOTrackedLink query
        needed here, the SOEvent row already carries the exact value, immune
        to how far the contact has progressed since (a click on step 0's
        link still carries step_order=0 no matter how many later steps have
        since been sent). The count is raw matching SOEvent rows — clicks on
        any OTHER step never contribute, and so_track_click has no dedup
        guard, so N clicks on the same link count as N, not 1 (documented
        choice, not an oversight — matches the field's own literal name).
        event_count_threshold is only ever read here; a no_event_after_days
        condition that happens to have it set ignores it completely, exactly
        like it already ignores everything else this function uses.
    RESOLVED (NO) — the click requirement is NOT satisfied AND wait_days
        have elapsed since source_step's OWN exact 'sent' timestamp
        (_source_step_sent_at, never cc.sent_at). Elapsed-time semantics
        (timedelta), matching the only day-based-wait pattern already used
        anywhere in this codebase (eligible_next_subsequence,
        _eval_no_event_after_days) — not calendar-day boundaries.

    Once resolved either way, the caller's last_condition_id exclusion (at
    both the eligibility-query and branch_via_condition's CAS-write level)
    is what stops this same condition from ever being found/applied again
    for this contact — a later click arriving after a NO branch has already
    committed cannot retroactively re-fire this condition (regardless of
    whether it would now satisfy the count threshold), because the
    condition is no longer even in the eligibility query's candidate set.
    """
    if condition.yes_target_step_id is None and condition.no_target_step_id is None:
        return None, None

    if _condition_satisfied(cc, condition):
        if condition.yes_target_step_id is None:
            return None, None
        return condition, condition.yes_target_step

    if condition.no_target_step_id is None:
        return None, None

    sent_at = _source_step_sent_at(cc, condition)
    if sent_at is None:
        # No exact sent-event timestamp for this step -- cannot safely
        # compute a NO-branch deadline without guessing, so don't.
        return None, None

    from datetime import timedelta
    cutoff = sent_at + timedelta(days=condition.wait_days)
    if now() < cutoff:
        return None, None
    return condition, condition.no_target_step


def _eval_opened(cc, condition):
    """trigger_type='opened' (V3.6 Phase 2) — identical three-state shape
    and event_count_threshold semantics to _eval_clicked, applied to
    'opened' SOEvent rows instead of 'clicked'. The only difference is the
    event_type filtered on; everything else (threshold fast-path, YES
    priority over timeout, NO via _source_step_sent_at, last_condition_id
    finality) is the same mechanism, deliberately not shared/refactored
    into one helper — see the module comment above for why
    no_event_after_days and clicked are already kept independent despite
    structural similarity.

    Exact attribution comes from SOEvent.step_order, which
    views/so_tracking.py::so_track_pixel populates straight from the opened
    SOOpenPixel's own step_order (V3.6 Phase 1) — a fresh artifact created
    per send, immune to how far the contact has progressed since. The
    LEGACY open pixel (so_track_open, cc.tracking_token, untouched) always
    produces step_order=NULL, which can never equal a real
    condition.source_step.order in this exact-match filter — so a legacy
    open can never satisfy an opened condition, structurally, not via a
    special case.

    Raw event count, no dedup, no fingerprinting, no proxy/bot filtering:
    every pixel hit counts individually toward event_count_threshold,
    exactly mirroring _eval_clicked's documented choice (matches the
    field's literal name; this codebase already accepts the analogous,
    if noisier, tradeoff for clicks).
    """
    if condition.yes_target_step_id is None and condition.no_target_step_id is None:
        return None, None

    if _condition_satisfied(cc, condition):
        if condition.yes_target_step_id is None:
            return None, None
        return condition, condition.yes_target_step

    if condition.no_target_step_id is None:
        return None, None

    sent_at = _source_step_sent_at(cc, condition)
    if sent_at is None:
        return None, None

    from datetime import timedelta
    cutoff = sent_at + timedelta(days=condition.wait_days)
    if now() < cutoff:
        return None, None
    return condition, condition.no_target_step


def _eval_replied(cc, condition):
    """trigger_type='replied' (V3.7) — the same three-state shape as
    _eval_clicked/_eval_opened, but with no count threshold: one genuine
    reply is always sufficient, so event_count_threshold is never read here
    (matches how no_event_after_days already ignores it completely — see
    the model's own docstring). YES still takes priority over the NO/
    timeout branch, exactly like clicked/opened.

    Excludes out-of-office auto-replies (SOEvent.metadata['oof']) — an
    autoresponder is not the prospect answering, so it must never satisfy
    the YES branch, mirroring services/so_imap.py's own rule that an OOO
    never stops the sequence either.

    Exact attribution comes from SOEvent.step_order, which
    services/so_imap.py::_record_once now computes precisely — for
    'replied' only — from the reply's own threading headers matched
    against the exact per-step 'sent' SOEvent history, falling back to the
    pre-V3.7 heuristic only when no precise match is possible. bounced/
    complained step attribution is untouched by that change.
    """
    if condition.yes_target_step_id is None and condition.no_target_step_id is None:
        return None, None

    if _condition_satisfied(cc, condition):
        if condition.yes_target_step_id is None:
            return None, None
        return condition, condition.yes_target_step

    if condition.no_target_step_id is None:
        return None, None

    sent_at = _source_step_sent_at(cc, condition)
    if sent_at is None:
        return None, None

    from datetime import timedelta
    cutoff = sent_at + timedelta(days=condition.wait_days)
    if now() < cutoff:
        return None, None
    return condition, condition.no_target_step


def eligible_condition_branch(cc):
    """Pure — no writes. Returns (condition, target_step) if `cc` should be
    routed by a SOSequenceCondition right now, else (None, None).

    Mirrors eligible_next_subsequence()'s gating exactly (status, sent_at),
    main-sequence-only, then tries the preemptive (no_event_after_days) and
    look-back (clicked, opened) matches in turn — see the module comment
    above for why each needs a different query shape. Only one is ever
    acted on per call, matching the dispatcher's own "try one mechanism,
    then the next" shape (so_dispatch_subsequence_branches tries the
    existing SOSubsequence check before this function at all).
    """
    if cc.status not in ('active', 'completed'):
        return None, None
    if not cc.sent_at:
        return None, None
    if cc.active_subsequence_id:
        return None, None

    preempt_condition = (
        cc.campaign.conditions
        .filter(
            is_active=True, trigger_type='no_event_after_days',
            source_step__isnull=False, source_step__order=cc.current_step,
        )
        .select_related('yes_target_step')
        .order_by('id')
        .first()
    )
    if preempt_condition is not None:
        result = _eval_no_event_after_days(cc, preempt_condition)
        if result != (None, None):
            return result

    # Unlike the preemptive match above (at most one current_step value can
    # ever be queried, so any matching condition IS the relevant one), a
    # range match can return several candidate conditions at once — e.g. one
    # campaign with a click condition at step 0 and an opened condition at
    # step 1, for a contact who has since reached step 2. Only ONE of them
    # may actually be resolved (the one whose specific engagement/timeout
    # condition is satisfied), so every candidate must be tried in turn
    # rather than stopping at the first one found.
    #
    # Deterministic priority (V3.4, widened in V3.6 Phase 2 to also cover
    # 'opened'): earliest source_step.order first, then lowest condition.id
    # as a tiebreak — ONE shared ordering across BOTH look-back trigger
    # types, not two independent per-type loops, so a mixed campaign (one
    # clicked condition, one opened condition, on different steps) still
    # resolves by step position alone, regardless of which trigger type
    # each one is. Earliest-step-first mirrors how a contact would actually
    # encounter these conditions while progressing through the sequence —
    # the condition attached to the step closest to where they started is
    # the more relevant one to resolve first — and this can only ever
    # matter when MULTIPLE conditions are simultaneously resolved at once
    # (e.g. two already-expired timeouts discovered on the same tick); a
    # condition that isn't yet resolved (its evaluator returns (None, None))
    # is skipped regardless of its priority, exactly like today.
    lookback_conditions = (
        cc.campaign.conditions
        .filter(
            is_active=True, trigger_type__in=('clicked', 'opened', 'replied'),
            source_step__isnull=False, source_step__order__lt=cc.current_step,
            group__isnull=True,   # V4.0 — a grouped condition is never evaluated
        )                          # standalone; see eligible_group_branch instead.
        .exclude(id=cc.last_condition_id)
        .select_related('yes_target_step', 'no_target_step', 'source_step')
        .order_by('source_step__order', 'id')
    )
    _LOOKBACK_EVALUATORS = {'clicked': _eval_clicked, 'opened': _eval_opened, 'replied': _eval_replied}
    for condition in lookback_conditions:
        evaluator = _LOOKBACK_EVALUATORS[condition.trigger_type]
        result = evaluator(cc, condition)
        if result != (None, None):
            return result

    return None, None


_BRANCH_PATH_MAX = 500


def _append_branch_path(existing, entry):
    """Append-only, bounded to SOCampaignContact.branch_path's max_length.
    Drops the oldest whole entries rather than truncating one mid-string."""
    combined = f'{existing}>{entry}' if existing else entry
    if len(combined) <= _BRANCH_PATH_MAX:
        return combined
    trimmed = combined[-_BRANCH_PATH_MAX:]
    first_sep = trimmed.find('>')
    return trimmed[first_sep + 1:] if first_sep != -1 else trimmed


def branch_via_condition(cc, condition, target_step):
    """Atomically move `cc` to `target_step` on the main sequence, per
    `condition`. Same compare-and-swap shape as branch_contact(): gated on
    the exact position (status, current_step, active_subsequence_id) this
    was evaluated against, so a concurrent evaluator — another dispatcher
    tick, or a concurrent attempt at the existing subsequence branch above —
    cannot double-branch this contact or overwrite a position it has already
    moved off of. branch_path's new value is derived from `cc`'s own
    in-memory snapshot, the same snapshot the WHERE clause is keyed against —
    if that snapshot is stale, the WHERE clause itself fails to match and
    NOTHING is written, so a stale branch_path can never land either.

    The extra `.exclude(last_condition_id=condition.id)` (V3.3) closes a gap
    the position-only WHERE clause alone doesn't cover: if target_step.order
    happens to equal the position being branched FROM (a real, plausible
    configuration for 'clicked' — e.g. "if clicked, stay near here" — unlike
    'no_event_after_days', whose targets are typically further along), the
    UPDATE doesn't actually change current_step's value, so two truly
    concurrent callers observing the same stale snapshot could otherwise
    both match and both apply — verified directly against this exact
    scenario. Once the first writer sets last_condition_id=condition.id,
    this exclusion alone stops the second from matching, independent of
    whether current_step's value moved. For every condition this WHERE
    clause has always correctly rejected anyway (a genuinely stale
    current_step/active_subsequence_id), cc.last_condition_id was never
    equal to condition.id in the first place, so this exclusion changes
    nothing there — confirmed by the full V3.1 regression suite.

    direction (V3.4): the branch_path entry records 'yes' or 'no' — inferred
    by comparing target_step against condition.yes_target_step/
    no_target_step rather than adding a parameter, so this signature (and
    every existing caller, including tasks/so_subsequence.py) stays
    unchanged. Falls back to 'yes' only if target_step matches neither
    (defensive; every current caller always passes one or the other).
    """
    from Email_validate_app.models import SOCampaign, SOCampaignContact

    if target_step.id == condition.no_target_step_id:
        direction = 'no'
    else:
        direction = 'yes'
    entry = f'main:{cc.current_step}>cond:{condition.id}:{direction}>main:{target_step.order}'
    new_branch_path = _append_branch_path(cc.branch_path, entry)

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status__in=('active', 'completed'),
        current_step=cc.current_step, active_subsequence_id=cc.active_subsequence_id,
    ).exclude(last_condition_id=condition.id).update(
        current_step=target_step.order, next_action_at=now(), status='active',
        last_condition_id=condition.id, branch_path=new_branch_path,
    )
    if updated:
        SOCampaign.objects.filter(id=cc.campaign_id, status='sent').update(status='sending')
        logger.info(
            'so_subsequence: campaign contact %s (%s) branched via condition %s to step %s',
            cc.id, cc.email, condition.id, target_step.order,
        )
    return bool(updated)


# ── V4.0 — AND/OR condition groups ──────────────────────────────────────────
#
# A SOConditionGroup combines 2+ existing SOSequenceCondition rows (all
# sharing the group's own source_step — enforced at the views/so_sender.py
# write path) with AND/OR logic. Deliberately mirrors the single-condition
# shape above exactly: a pure eligible_group_branch()/_eval_group() (no
# writes) plus a CAS branch_via_group() (same WHERE-clause shape as
# branch_via_condition, just keyed on last_condition_group_id instead of
# last_condition_id — see SOCampaignContact.last_condition_group_id's own
# comment for why that's a separate field rather than overloading the
# existing one). Grouped conditions never appear in eligible_condition_branch
# above (its lookback query now excludes group__isnull=False) — every member
# condition's own yes_target_step/no_target_step is unused once grouped (the
# group is the sole owner of the branch decision), which is itself why a
# grouped condition would be a no-op there even without that filter (see
# _eval_clicked/_eval_opened/_eval_replied's own first-line guard).
#
# 'no_event_after_days' is never a valid group member (see SOConditionGroup's
# own docstring) — _condition_satisfied has no meaningful predicate for it,
# and views/so_sender.py's write-path validation rejects it outright, so
# _eval_group below never needs to special-case it.

def _eval_group(cc, group):
    """Pure — no writes. Same three-state shape as _eval_clicked/_eval_opened/
    _eval_replied (WAITING -> (None, None), RESOLVED YES, RESOLVED NO), but
    the YES predicate is the AND/OR combination of every member condition's
    own _condition_satisfied() result rather than a single trigger's — and
    the NO/timeout deadline is measured from the group's OWN wait_days since
    the shared source_step's sent timestamp (_step_sent_at), never any one
    member's individual wait_days (unused once a condition is grouped, same
    as its unused yes_target_step/no_target_step)."""
    if group.yes_target_step_id is None and group.no_target_step_id is None:
        return None, None

    members = list(group.conditions.all())
    if not members:
        # A group with fewer than 2 members should never exist post-launch
        # (write-path validation requires 2+), but this stays defensive
        # rather than assuming — an empty/mid-edit group is simply never
        # eligible, exactly like a condition with no targets configured.
        return None, None

    satisfied = [_condition_satisfied(cc, member) for member in members]
    resolved = all(satisfied) if group.logic == 'and' else any(satisfied)

    if resolved:
        if group.yes_target_step_id is None:
            return None, None
        return group, group.yes_target_step

    if group.no_target_step_id is None:
        return None, None

    sent_at = _step_sent_at(cc, group.source_step)
    if sent_at is None:
        return None, None

    from datetime import timedelta
    cutoff = sent_at + timedelta(days=group.wait_days)
    if now() < cutoff:
        return None, None
    return group, group.no_target_step


def eligible_group_branch(cc):
    """Pure — no writes. Returns (group, target_step) if `cc` should be
    routed by a SOConditionGroup right now, else (None, None). Mirrors
    eligible_condition_branch()'s gating and lookback-query shape exactly —
    groups only ever occupy the look-back query space (member trigger types
    are restricted to clicked/opened/replied, never the preemptive
    no_event_after_days — see SOConditionGroup's own docstring), so there is
    no preemptive-match half here the way eligible_condition_branch has.

    The member prefetch below also select_relateds each member's OWN
    source_step — _eval_group's per-member _condition_satisfied() call reads
    member.source_step.order (V4.0 adversarial audit's Low-Medium N+1
    finding: a plain prefetch_related('conditions') left that access
    triggering one fresh query per member; a nested Prefetch queryset with
    select_related fixes it without changing which members/data are read)."""
    from django.db.models import Prefetch
    from Email_validate_app.models import SOSequenceCondition

    if cc.status not in ('active', 'completed'):
        return None, None
    if not cc.sent_at:
        return None, None
    if cc.active_subsequence_id:
        return None, None

    groups = (
        cc.campaign.groups
        .filter(is_active=True, source_step__isnull=False, source_step__order__lt=cc.current_step)
        .exclude(id=cc.last_condition_group_id)
        .select_related('yes_target_step', 'no_target_step', 'source_step')
        .prefetch_related(
            Prefetch('conditions', queryset=SOSequenceCondition.objects.select_related('source_step'))
        )
        .order_by('source_step__order', 'id')
    )
    for group in groups:
        result = _eval_group(cc, group)
        if result != (None, None):
            return result

    return None, None


def branch_via_group(cc, group, target_step):
    """Atomically move `cc` to `target_step` on the main sequence, per
    `group`. Byte-for-byte the same CAS shape as branch_via_condition() —
    see that function's own docstring for the full race-safety reasoning,
    which applies here unchanged — the only difference is which id gets
    excluded/recorded (last_condition_group_id, not last_condition_id) and
    which branch_path hop format gets written ('grp:', not 'cond:', so V4.2
    analytics can tell the two apart — see services/so_analytics.py)."""
    from Email_validate_app.models import SOCampaign, SOCampaignContact

    if target_step.id == group.no_target_step_id:
        direction = 'no'
    else:
        direction = 'yes'
    entry = f'main:{cc.current_step}>grp:{group.id}:{direction}>main:{target_step.order}'
    new_branch_path = _append_branch_path(cc.branch_path, entry)

    updated = SOCampaignContact.objects.filter(
        id=cc.id, status__in=('active', 'completed'),
        current_step=cc.current_step, active_subsequence_id=cc.active_subsequence_id,
    ).exclude(last_condition_group_id=group.id).update(
        current_step=target_step.order, next_action_at=now(), status='active',
        last_condition_group_id=group.id, branch_path=new_branch_path,
    )
    if updated:
        SOCampaign.objects.filter(id=cc.campaign_id, status='sent').update(status='sending')
        logger.info(
            'so_subsequence: campaign contact %s (%s) branched via group %s to step %s',
            cc.id, cc.email, group.id, target_step.order,
        )
    return bool(updated)


def eligible_branch(cc):
    """Pure — no writes. The single combined entry point the dispatcher
    should use: merges eligible_condition_branch() and eligible_group_branch()
    into ONE priority ordering, returning ('condition', condition, target),
    ('group', group, target), or (None, None, None).

    The preemptive (no_event_after_days) match inside eligible_condition_branch
    always wins outright, unconditionally, exactly as it already does today —
    groups can never contain a no_event_after_days member (see
    SOConditionGroup's own docstring), so they never compete for that match
    at all; detecting it needs no new query, just checking the trigger_type
    of whatever eligible_condition_branch already returned. Otherwise (no
    preempt resolution — both candidates, if any, came from each function's
    own look-back space), the two functions' own results are compared by the
    exact same (source_step__order, id) tie-break each already uses
    internally, so a mixed campaign (some standalone conditions, some
    groups) still resolves by step position alone, regardless of which kind
    the winning candidate is. This function itself still returns exactly
    one candidate per call — the caller (tasks/so_subsequence.py, V4.1) is
    what decides how many times to call it per tick; see that module's own
    docstring for its same-tick cascading loop.
    """
    condition, target = eligible_condition_branch(cc)
    if condition is not None and condition.trigger_type == 'no_event_after_days':
        return 'condition', condition, target

    group, group_target = eligible_group_branch(cc)

    if condition is None and group is None:
        return None, None, None
    if group is None:
        return 'condition', condition, target
    if condition is None:
        return 'group', group, group_target

    cond_key  = (condition.source_step.order, condition.id)
    group_key = (group.source_step.order, group.id)
    if cond_key <= group_key:
        return 'condition', condition, target
    return 'group', group, group_target
