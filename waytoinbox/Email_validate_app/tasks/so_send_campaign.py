import logging

from celery import shared_task
from django.core.cache import cache
from django.utils.timezone import now

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT     = 1800   # 30 min — enrollment lock
_LOCK_PREFIX      = 'so_send_campaign'
_DISPATCH_LOCK    = 'so_dispatch_due_sequence_steps'
_DISPATCH_TIMEOUT = 240    # dispatcher runs every minute; guard against overlap
_STUCK_MINUTES    = 30
_DISPATCH_BATCH   = 100


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_send_campaign.so_send_campaign_task',
    max_retries=0,
    soft_time_limit=1700,
    time_limit=1800,
    base=LoggedTask,
)
def so_send_campaign_task(self, campaign_id):
    """Launch a campaign: resolve its audience and snapshot one SOCampaignContact
    per recipient, ready for the sequence dispatcher to pick up.

    This task does NOT send anything itself. Step 1 fires on the next
    so_dispatch_due_sequence_steps tick (within ~60s), keeping exactly one send
    code path (services/so_drip.py) shared by every step instead of duplicating
    the SMTP loop here for step 1 only.
    """
    from Email_validate_app.models import (
        SOCampaign, SOCampaignContact, SOListProspect, SOEvent, SOProspect,
    )
    from Email_validate_app.services.so_segment_builder import build_so_segment_queryset
    from Email_validate_app.services.so_drip import pick_sender_account

    lock_key = f'{_LOCK_PREFIX}:{campaign_id}'
    if not cache.add(lock_key, '1', timeout=_LOCK_TIMEOUT):
        logger.info('so_send: campaign %s already locked, skipping', campaign_id)
        return {'status': 'skipped'}

    try:
        try:
            campaign = (
                SOCampaign.objects
                .prefetch_related('recipient_lists', 'recipient_segments',
                                   'exclude_lists', 'exclude_segments',
                                   'account_rotations__account')
                .get(id=campaign_id)
            )
        except SOCampaign.DoesNotExist:
            logger.error('so_send: campaign %s not found', campaign_id)
            return {'status': 'not_found'}

        if campaign.status in ('sent', 'failed', 'cancelled'):
            logger.info('so_send: campaign %s already in terminal state %s',
                       campaign_id, campaign.status)
            return {'status': 'terminal'}

        # account_rotations was prefetched above; .all() respects the model's
        # default ordering (['order']). Built once, reused both as the
        # "does this campaign have anywhere to send from" gate and as the
        # pool each new contact is assigned across below — kept as
        # SOEmailAccountRotation objects, not just .account, so each one's
        # own .daily_send_count is available to pick_sender_account. Only
        # connected accounts participate — a failed/unchecked account must
        # never be handed out to a brand-new contact.
        rotations = [
            r for r in campaign.account_rotations.all()
            if not r.account.deleted_at and r.account.status == 'connected'
        ]
        if not rotations:
            logger.error('so_send: no valid email account for campaign %s', campaign_id)
            SOCampaign.objects.filter(id=campaign_id).update(status='failed', updated_at=now())
            return {'status': 'no_account'}

        # ── Resolve audience: lists + segments, minus exclusions, minus prior
        # bounces, subscribed prospects only. ──────────────────────────────────
        candidates = {}   # email -> SOProspect
        for lst in campaign.recipient_lists.filter(deleted_at__isnull=True):
            for lp in SOListProspect.objects.filter(so_list=lst).select_related('prospect'):
                p = lp.prospect
                if p.deleted_at or p.status != 'subscribed':
                    continue
                candidates[p.email] = p

        for seg in campaign.recipient_segments.filter(deleted_at__isnull=True):
            for p in build_so_segment_queryset(seg, campaign.user_id).filter(status='subscribed'):
                candidates[p.email] = p

        excluded_emails = set()
        for lst in campaign.exclude_lists.filter(deleted_at__isnull=True):
            excluded_emails.update(
                SOListProspect.objects.filter(so_list=lst, prospect__deleted_at__isnull=True)
                .values_list('prospect__email', flat=True)
            )
        for seg in campaign.exclude_segments.filter(deleted_at__isnull=True):
            try:
                excluded_emails.update(
                    build_so_segment_queryset(seg, campaign.user_id).values_list('email', flat=True)
                )
            except Exception:
                continue

        # Bounce and complaint are both durable, cross-campaign do-not-mail
        # signals for this user — a fresh campaign must not enroll either.
        suppressed_emails = set(
            SOEvent.objects.filter(
                campaign__user_id=campaign.user_id, event_type__in=('bounced', 'complained'),
            ).values_list('email', flat=True)
        )

        prospect_map = {
            email: p for email, p in candidates.items()
            if email not in excluded_emails and email not in suppressed_emails and '@' in email
        }

        logger.info('so_send: campaign %s launched — %s eligible recipient(s) '
                   '(%s excluded, %s bounced/complained)',
                   campaign_id, len(prospect_map), len(excluded_emails & set(candidates)),
                   len(suppressed_emails & set(candidates)))

        if not prospect_map:
            SOCampaign.objects.filter(id=campaign_id).update(
                status='sent', sent_at=now(), updated_at=now(),
            )
            return {'status': 'no_recipients'}

        # ── Snapshot: one SOCampaignContact per recipient, sequence-ready. ─────
        existing_emails = set(
            SOCampaignContact.objects.filter(campaign_id=campaign_id).values_list('email', flat=True)
        )
        start_at = campaign.schedule_at or now()
        new_ccs = []
        for email, prospect in prospect_map.items():
            if email in existing_emails:
                continue
            new_ccs.append(SOCampaignContact(
                campaign_id=campaign_id,
                prospect=prospect,
                email=email,
                status='active',
                current_step=0,
                # V4.x variation-weight fix — deliberately '' (never the
                # model's own 'A' default), the permanent per-contact
                # sentinel meaning "use per-step deterministic weighted
                # selection" (see services/so_drip.py::_resolve_step_and_
                # variant). No longer pre-picked here against step 0's
                # variants alone — each step now resolves independently, at
                # send time, against its OWN configured weights.
                variant_label='',
                next_action_at=start_at,
                # Sticky account assignment across the campaign's selected
                # accounts, deterministic per (campaign, email) so a retried
                # enrollment lands on the same account rather than re-rolling
                # it — this recipient's whole sequence then sends from this
                # same account for every subsequent step (see
                # so_drip.py::_get_contact_account, which just returns
                # cc.account once it's set). Weighted by each account's
                # Campaign Sending Count when the campaign has that toggle
                # on, uniform otherwise — see pick_sender_account.
                account=pick_sender_account(
                    campaign_id, email, rotations, campaign.sender_send_count_enabled),
            ))
        if new_ccs:
            SOCampaignContact.objects.bulk_create(new_ccs, ignore_conflicts=True)
            logger.info('so_send: campaign %s enrolled %s new contact(s)', campaign_id, len(new_ccs))

        if campaign.status != 'sending':
            SOCampaign.objects.filter(id=campaign_id).update(status='sending', updated_at=now())

        return {'status': 'ok', 'enrolled': len(new_ccs)}
    finally:
        cache.delete(lock_key)


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_send_campaign.so_dispatch_due_sequence_steps',
    max_retries=0,
    soft_time_limit=280,
    time_limit=300,
    base=LoggedTask,
)
def so_dispatch_due_sequence_steps(self):
    """Beat task, every minute — mirrors so_dispatch_scheduled_campaigns's
    claim-by-conditional-UPDATE pattern, but at the per-recipient level.

    Finds SOCampaignContact rows that are due for their next step, claims each
    atomically (only one worker can move a row active -> sending), sends via
    services/so_drip.send_next_step, then finalises any campaign whose contacts
    have all reached a terminal state.
    """
    from Email_validate_app.models import SOCampaignContact, SOCampaign
    from Email_validate_app.services.so_drip import send_next_step

    if not cache.add(_DISPATCH_LOCK, '1', timeout=_DISPATCH_TIMEOUT):
        return {'status': 'skipped'}

    try:
        due = list(
            SOCampaignContact.objects
            .filter(status='active', next_action_at__lte=now(),
                   campaign__status='sending', campaign__deleted_at__isnull=True)
            .select_related('campaign', 'prospect', 'active_subsequence')
            .order_by('next_action_at')[:_DISPATCH_BATCH]
        )

        sent = skipped = 0
        touched_campaign_ids = set()
        for cc in due:
            claimed = SOCampaignContact.objects.filter(id=cc.id, status='active').update(status='sending')
            if not claimed:
                skipped += 1   # another worker already took this one
                continue
            # Re-fetch fresh after the claim. The claim only guarantees
            # exclusivity of the status transition — any other field
            # (current_step, active_subsequence, account, attempts) may have
            # been changed by a concurrent writer (e.g. a subsequence branch
            # from so_dispatch_subsequence_branches, which deliberately leaves
            # status='active' so this claim still succeeds) between the batch
            # snapshot above and this row's turn in the loop. Acting on the
            # pre-claim in-memory object would send stale content and then
            # clobber that concurrent write with a stale current_step.
            cc = SOCampaignContact.objects.select_related(
                'campaign', 'prospect', 'active_subsequence',
            ).get(id=cc.id)
            touched_campaign_ids.add(cc.campaign_id)
            logger.info('so_dispatch: campaign %s contact %s (%s) due for step %s',
                       cc.campaign_id, cc.id, cc.email, cc.current_step)
            if send_next_step(cc):
                sent += 1

        # Finalise any touched campaign that has no active/sending contacts left.
        for campaign_id in touched_campaign_ids:
            still_running = SOCampaignContact.objects.filter(
                campaign_id=campaign_id, status__in=('active', 'sending'),
            ).exists()
            if not still_running:
                updated = SOCampaign.objects.filter(id=campaign_id, status='sending').update(
                    status='sent', sent_at=now(), updated_at=now(),
                )
                if updated:
                    logger.info('so_dispatch: campaign %s completed — all contacts finished', campaign_id)

        return {'status': 'ok', 'sent': sent, 'skipped': skipped, 'due': len(due)}
    finally:
        cache.delete(_DISPATCH_LOCK)


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_send_campaign.so_dispatch_scheduled_campaigns',
    max_retries=0,
    base=LoggedTask,
)
def so_dispatch_scheduled_campaigns(self):
    from Email_validate_app.models import SOCampaign
    due = SOCampaign.objects.filter(
        status='scheduled', deleted_at__isnull=True, schedule_at__lte=now()
    )
    dispatched = 0
    for campaign in due:
        claimed = SOCampaign.objects.filter(id=campaign.id, status='scheduled').update(
            status='sending', updated_at=now()
        )
        if claimed:
            so_send_campaign_task.delay(campaign.id)
            dispatched += 1
    return {'dispatched': dispatched}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_send_campaign.so_recover_stuck_campaigns',
    max_retries=0,
    base=LoggedTask,
)
def so_recover_stuck_campaigns(self):
    """A campaign in 'sending' with no enrolled contacts and a stale updated_at
    means the launch task died before enrolling anyone — that's the only case
    this recovers. A campaign correctly waiting between sequence steps also sits
    in 'sending' with a stale updated_at for perfectly legitimate reasons (the
    wait itself), so liveness is judged by whether it still has active/sending
    contacts, never by updated_at once enrollment has happened.
    """
    from datetime import timedelta
    from Email_validate_app.models import SOCampaign, SOCampaignContact
    cutoff = now() - timedelta(minutes=_STUCK_MINUTES)

    candidates = SOCampaign.objects.filter(
        status='sending', deleted_at__isnull=True, sent_at__isnull=True, updated_at__lt=cutoff,
    )
    recovered = 0
    for campaign in candidates:
        lock_key = f'{_LOCK_PREFIX}:{campaign.id}'
        if cache.get(lock_key):
            continue
        has_contacts = SOCampaignContact.objects.filter(campaign_id=campaign.id).exists()
        if has_contacts:
            # Enrollment happened; any waiting is between-step, not stuck.
            continue
        SOCampaign.objects.filter(id=campaign.id, status='sending').update(
            status='failed', updated_at=now()
        )
        recovered += 1
    return {'recovered': recovered}
