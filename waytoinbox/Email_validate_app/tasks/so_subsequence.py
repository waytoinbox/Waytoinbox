import logging

from celery import shared_task
from django.core.cache import cache

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

_LOCK           = 'so_dispatch_subsequence_branches'
_LOCK_TIMEOUT   = 840   # runs every 15 min; guard against overlap
_BATCH          = 200


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_subsequence.so_dispatch_subsequence_branches',
    max_retries=0,
    base=LoggedTask,
)
def so_dispatch_subsequence_branches(self):
    """Move contacts with no reply for long enough onto their campaign's next
    chained subsequence. Day-granularity trigger, so 15-minute polling (same
    cadence as IMAP sync) is plenty — no need for the main dispatcher's
    per-minute precision.

    Candidates can sit in a campaign already finalized to status='sent' (that
    happens the moment every contact finishes the main sequence, which is the
    common case right before a no-reply trigger fires) — so this scans both
    'sending' and 'sent' campaigns; branch_contact() re-opens the campaign
    when it actually moves someone, so so_dispatch_due_sequence_steps picks
    the contact back up on its own next tick.
    """
    from Email_validate_app.models import SOCampaignContact
    from Email_validate_app.services.so_subsequence import eligible_next_subsequence, branch_contact

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
                campaign__subsequences__is_active=True,
            )
            .select_related('campaign', 'active_subsequence')
            .distinct()[:_BATCH]
        )

        branched = 0
        for cc in candidates:
            next_sub = eligible_next_subsequence(cc)
            if next_sub is None:
                continue
            if branch_contact(cc, next_sub):
                branched += 1

        return {'status': 'ok', 'branched': branched, 'candidates': len(candidates)}
    finally:
        cache.delete(_LOCK)
