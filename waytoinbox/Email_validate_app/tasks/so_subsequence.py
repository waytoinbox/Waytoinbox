import logging

from celery import shared_task
from django.core.cache import cache
from django.db.models import Q

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

_LOCK           = 'so_dispatch_subsequence_branches'
_LOCK_TIMEOUT   = 840   # runs every 15 min; guard against overlap
_BATCH          = 200
# V4.1 — hard safety cap on same-tick cascading, not a target. Bounds a
# contact to at most this many branch hops within ONE dispatcher execution,
# so a misconfigured branch cycle (e.g. step 2 -> step 4 -> step 2 -> ...)
# terminates instead of looping — see so_dispatch_subsequence_branches'
# own docstring for the full reasoning. Purely local to this function's
# execution; never persisted anywhere (not on SOCampaignContact, not
# cached) — a fresh count starts for every contact, every tick.
MAX_SAME_TICK_BRANCH_HOPS = 10


def _refetch_contact(cc):
    """V4.1 — re-read a contact fresh after a successful hop. The CAS write
    inside branch_contact()/branch_via_condition()/branch_via_group() only
    guarantees the DATABASE is correct; it never mutates the in-memory `cc`
    object handed to it. eligible_next_subsequence()/eligible_branch() are
    pure functions of whatever `cc` they're given, so acting on a stale
    in-memory object after a hop would evaluate the WRONG position — the
    exact same reasoning already documented in
    so_dispatch_due_sequence_steps for its own single re-fetch after a claim,
    just applied once per hop here instead of once per tick."""
    from Email_validate_app.models import SOCampaignContact

    return SOCampaignContact.objects.select_related('campaign', 'active_subsequence').get(id=cc.id)


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_subsequence.so_dispatch_subsequence_branches',
    max_retries=0,
    base=LoggedTask,
)
def so_dispatch_subsequence_branches(self):
    """Move contacts with no reply for long enough onto their campaign's next
    chained subsequence, OR — V3.1 — onto a SOSequenceCondition's configured
    target step. Day-granularity trigger, so 15-minute polling (same cadence
    as IMAP sync) is plenty — no need for the main dispatcher's per-minute
    precision.

    Candidates can sit in a campaign already finalized to status='sent' (that
    happens the moment every contact finishes the main sequence, which is the
    common case right before a no-reply trigger fires) — so this scans both
    'sending' and 'sent' campaigns; branch_contact()/branch_via_condition()/
    branch_via_group() re-open the campaign when they actually move someone,
    so so_dispatch_due_sequence_steps picks the contact back up on its own
    next tick.

    The existing SOSubsequence check runs first, unchanged; the combined
    condition/group check (V4.0 — services/so_subsequence.py::eligible_branch,
    which merges eligible_condition_branch()/eligible_group_branch() into one
    priority ordering) is tried whenever the subsequence check didn't branch.
    A campaign with zero SOSequenceCondition/SOConditionGroup rows takes
    exactly the code path it took before V3.1 existed; the OR in the
    candidates filter below only widens which contacts are even fetched, it
    changes no existing candidate's outcome.

    V4.1 — SAME-TICK CASCADING: a contact can now take up to
    MAX_SAME_TICK_BRANCH_HOPS branch actions within this one execution
    (previously: at most one, with a second already-resolvable hop having to
    wait for the next 15-minute tick). Nothing about HOW a single hop is
    decided or committed changes — every hop still goes through the exact
    same unmodified eligible_next_subsequence()/eligible_branch() and
    branch_contact()/branch_via_condition()/branch_via_group() calls, in the
    same order, evaluated against a freshly re-fetched contact
    (_refetch_contact) each time. The loop stops the moment any of the
    following is true: nothing is eligible any more (natural end of chain);
    a branch attempt's CAS fails (0 rows updated — some concurrent writer,
    e.g. a bounce landing mid-cascade, changed the contact; give up rather
    than retry or guess, exactly like a single-hop tick already does); the
    contact's campaign is no longer 'sending'/'sent' (a NEW check, additive
    only to this loop — none of the eligible_* functions have ever checked
    campaign.status, since that gate previously only needed to run once, in
    the candidates query below, before a contact's single hop; under
    cascading, an unwatched pause/cancel between hop 2 and hop 10 would
    otherwise widen that pre-existing, already-accepted one-hop race window
    to a ten-hop one, which this check closes without touching campaign
    status semantics anywhere else); or the hop cap is reached (cycle
    safety — a same-tick cycle simply consumes the cap and stops, no
    graph-cycle-detection needed). A contact's own status becoming anything
    other than active/completed (bounce/complaint/cancel/stop) is already
    caught for free by eligible_next_subsequence()/eligible_branch()'s own
    unchanged first-line gate once re-fetched — no new check needed for
    those. Suppression (bounce/complaint/unsubscribe), quota, and account
    rotation are untouched: branching never sends anything, so cascading a
    contact through several hops in one tick is exactly as safe as spreading
    those same hops across several ticks — the actual send-time gate in
    services/so_drip.py::send_next_step is unchanged and unreached by any of
    this.
    """
    from Email_validate_app.models import SOCampaignContact
    from Email_validate_app.services.so_subsequence import (
        eligible_next_subsequence, branch_contact,
        eligible_branch, branch_via_condition, branch_via_group,
    )

    if not cache.add(_LOCK, '1', timeout=_LOCK_TIMEOUT):
        return {'status': 'skipped'}

    try:
        candidates = (
            SOCampaignContact.objects
            .filter(
                status__in=('active', 'completed'),
                sent_at__isnull=False,
                campaign__status__in=('sending', 'sent'),
                campaign__deleted_at__isnull=True,
            )
            .filter(
                Q(campaign__subsequences__is_active=True)
                | Q(campaign__conditions__is_active=True)
                | Q(campaign__groups__is_active=True)
            )
            .select_related('campaign', 'active_subsequence')
            .distinct()[:_BATCH]
        )

        branched = 0
        for cc in candidates:
            hops = 0
            while hops < MAX_SAME_TICK_BRANCH_HOPS:
                if cc.campaign.status not in ('sending', 'sent'):
                    break

                next_sub = eligible_next_subsequence(cc)
                if next_sub is not None:
                    if not branch_contact(cc, next_sub):
                        break   # lost the CAS to a concurrent writer -- defer to the next tick
                    branched += 1
                    hops += 1
                    cc = _refetch_contact(cc)
                    continue

                kind, obj, target_step = eligible_branch(cc)
                if kind == 'condition':
                    if not branch_via_condition(cc, obj, target_step):
                        break
                    branched += 1
                    hops += 1
                    cc = _refetch_contact(cc)
                    continue
                if kind == 'group':
                    if not branch_via_group(cc, obj, target_step):
                        break
                    branched += 1
                    hops += 1
                    cc = _refetch_contact(cc)
                    continue

                break   # nothing eligible at all -- cascade ends naturally

        return {'status': 'ok', 'branched': branched, 'candidates': len(candidates)}
    finally:
        cache.delete(_LOCK)
