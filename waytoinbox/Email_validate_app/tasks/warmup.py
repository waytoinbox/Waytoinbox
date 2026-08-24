"""
Warmup Celery tasks. Follows the Sales Outreach convention exactly
(tasks/so_inbox_sync.py, tasks/so_send_campaign.py): base=LoggedTask,
max_retries=0 everywhere (no self.retry()), dispatcher-with-no-lock +
per-unit-worker-with-cache.add()-lock fan-out, conditional-UPDATE
(filter(status=X).update(status=Y)) as the idempotency backbone, and a
dedicated stuck-row recovery sweep instead of Celery-level retry/backoff.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

_STUCK_MINUTES = 20   # generous vs. the 30-45s soft/hard time limits below — only catches worker crashes


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.warmup.warmup_dispatch_sends',
    max_retries=0,
    base=LoggedTask,
)
def warmup_dispatch_sends(self):
    """Dispatcher — tops up today's pending queue for every active warmup,
    then fans each due message out to its own send task. No lock on the
    dispatcher itself (cheap/idempotent to re-run); the per-message claim in
    warmup_send_one is what makes fan-out safe."""
    from Email_validate_app.models import SOEmailAccountWarmup, WarmupMessage
    from Email_validate_app.services import warmup as warmup_service

    active_warmups = SOEmailAccountWarmup.objects.filter(
        status='active', account__status='connected', account__deleted_at__isnull=True,
    ).select_related('account')

    created_total = 0
    for warmup in active_warmups:
        try:
            created_total += warmup_service.create_pending_messages_for_sender(warmup)
        except Exception as exc:
            # One sender's enrollment/config problem must not block the rest.
            logger.error('warmup_dispatch_sends: create_pending_messages failed for account %s: %s',
                         warmup.account_id, exc)

    due_ids = list(WarmupMessage.objects.filter(
        status='pending', scheduled_for__lte=now(), sender_account__warmup__status='active',
    ).values_list('id', flat=True))

    for message_id in due_ids:
        warmup_send_one.delay(message_id)

    return {'created': created_total, 'dispatched': len(due_ids)}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.warmup.warmup_send_one',
    max_retries=0,
    soft_time_limit=30,
    time_limit=45,
    base=LoggedTask,
)
def warmup_send_one(self, message_id):
    """Claims one pending WarmupMessage and sends it. Re-checks the sender's
    live warmup status before actually sending — this is what makes Pause
    take effect immediately even for a message already queued/dispatched
    before the pause happened."""
    import smtplib
    from Email_validate_app.models import WarmupMessage
    from Email_validate_app.services import warmup as warmup_service
    from Email_validate_app.services.warmup_sender import send_warmup_email

    claimed = WarmupMessage.objects.filter(id=message_id, status='pending').update(status='sending')
    if not claimed:
        return {'status': 'skipped_already_claimed'}

    message = WarmupMessage.objects.select_related('sender_account', 'sender_account__warmup').filter(
        id=message_id,
    ).first()
    if not message or not message.sender_account:
        WarmupMessage.objects.filter(id=message_id, status='sending').update(
            status='send_failed', error='sender account no longer exists',
        )
        return {'status': 'sender_missing'}

    warmup = getattr(message.sender_account, 'warmup', None)
    if not warmup or warmup.status != 'active':
        # Paused/stopped since this message was queued — revert without sending.
        WarmupMessage.objects.filter(id=message_id, status='sending').update(status='pending')
        return {'status': 'skipped_not_active'}

    if not warmup_service.reserve_quota_slot(message.sender_account, warmup):
        WarmupMessage.objects.filter(id=message_id, status='sending').update(status='pending')
        return {'status': 'skipped_quota_exhausted'}

    try:
        send_warmup_email(message)
    except smtplib.SMTPAuthenticationError as exc:
        # Terminal immediately — auth won't fix itself on retry.
        warmup_service.release_quota_slot(message.sender_account)
        message.send_attempts += 1
        message.status = 'send_failed'
        message.error   = f'SMTP auth failed: {exc}'
        message.save(update_fields=['send_attempts', 'status', 'error', 'updated_at'])
        return {'status': 'send_auth_failed'}
    except Exception as exc:
        warmup_service.release_quota_slot(message.sender_account)
        message.send_attempts += 1
        if message.send_attempts >= settings.WARMUP_MAX_SEND_ATTEMPTS:
            message.status = 'send_failed'
        else:
            message.status = 'pending'   # retried on the next dispatch tick
        message.error = str(exc)
        message.save(update_fields=['send_attempts', 'status', 'error', 'updated_at'])
        return {'status': 'send_error'}

    message.status      = 'sent'
    message.check_after = now() + timedelta(minutes=settings.WARMUP_INITIAL_CHECK_DELAY_MINUTES)
    message.save(update_fields=['status', 'subject', 'message_id', 'sent_at', 'check_after', 'updated_at'])

    warmup_check_one.apply_async(args=[message.id], eta=message.check_after)
    return {'status': 'sent'}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.warmup.warmup_dispatch_checks',
    max_retries=0,
    base=LoggedTask,
)
def warmup_dispatch_checks(self):
    """Safety-net sweep for messages whose check is due, in case an
    eta-scheduled warmup_check_one was lost (worker restart, Beat downtime).
    Redundant with the per-message eta call by design — the claim inside
    warmup_check_one makes double-dispatch harmless."""
    from Email_validate_app.models import WarmupMessage

    due_ids = list(WarmupMessage.objects.filter(
        status='sent', check_after__lte=now(),
    ).values_list('id', flat=True)[:500])

    for message_id in due_ids:
        warmup_check_one.delay(message_id)

    return {'dispatched': len(due_ids)}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.warmup.warmup_check_one',
    max_retries=0,
    soft_time_limit=30,
    time_limit=45,
    base=LoggedTask,
)
def warmup_check_one(self, message_id):
    """Claims one sent WarmupMessage and checks its Gmail landing location.
    landing_location always records the ORIGINAL classification and is
    never overwritten by a later spam rescue — rescued_to_inbox is the
    separate fact that the move happened."""
    from Email_validate_app.models import WarmupMessage
    from Email_validate_app.services import warmup_receiver
    from Email_validate_app.services.warmup_receiver import ReceiverAuthError, GmailApiError

    claimed = WarmupMessage.objects.filter(id=message_id, status='sent').update(status='checking')
    if not claimed:
        return {'status': 'skipped_already_claimed'}

    message = WarmupMessage.objects.select_related('receiver_account').filter(id=message_id).first()
    if not message:
        return {'status': 'message_missing'}

    receiver = message.receiver_account
    if not receiver or receiver.status != 'connected':
        # Receiver-side problem, not evidence the email is missing — revert
        # for a later retry once the user reconnects, no attempt penalty.
        WarmupMessage.objects.filter(id=message_id, status='checking').update(status='sent')
        return {'status': 'skipped_receiver_unavailable'}

    try:
        service = warmup_receiver.get_gmail_service(receiver)
        found = warmup_receiver.find_warmup_message(service, message.identifier)
    except ReceiverAuthError as exc:
        from Email_validate_app.models import WarmupReceiverAccount
        WarmupReceiverAccount.objects.filter(id=receiver.id).update(status='revoked')
        WarmupMessage.objects.filter(id=message_id, status='checking').update(status='sent')
        logger.warning('warmup_check_one: receiver %s revoked: %s', receiver.email, exc)
        return {'status': 'receiver_revoked'}
    except (GmailApiError, EnvironmentError) as exc:
        return _defer_or_terminate_not_found(message, str(exc))

    from Email_validate_app.models import WarmupReceiverAccount
    WarmupReceiverAccount.objects.filter(id=receiver.id).update(last_checked_at=now())

    if found is None:
        return _defer_or_terminate_not_found(message, '')

    label_ids   = found['labelIds']
    was_unread  = 'UNREAD' in label_ids
    location    = warmup_receiver.classify_landing(label_ids)

    message.was_unread = was_unread

    if location == 'inbox':
        message.landing_location = 'inbox'
        message.rescued_to_inbox = False
        if was_unread:
            try:
                warmup_receiver.mark_as_read(service, found['id'])
                message.marked_read = True
            except (ReceiverAuthError, GmailApiError) as exc:
                message.error = f'mark_as_read failed: {exc}'

    elif location == 'spam':
        # Save the original classification before attempting the rescue —
        # if the rescue call itself then fails, detection is still recorded.
        message.landing_location = 'spam'
        try:
            warmup_receiver.rescue_from_spam(service, found['id'], was_unread)
            message.rescued_to_inbox = True
            message.marked_read      = was_unread
        except (ReceiverAuthError, GmailApiError) as exc:
            message.rescued_to_inbox = False
            message.error = f'rescue_from_spam failed: {exc}'

    else:  # 'other' — left completely untouched, not moved, not marked read
        message.landing_location = 'other'
        message.rescued_to_inbox = False

    message.status     = 'completed'
    message.checked_at = now()
    message.save(update_fields=[
        'was_unread', 'landing_location', 'rescued_to_inbox', 'marked_read',
        'status', 'checked_at', 'error', 'updated_at',
    ])
    return {'status': 'completed', 'landing_location': message.landing_location}


def _defer_or_terminate_not_found(message, error_note):
    """Shared not-found/API-error handling: bounded retries with increasing
    backoff, then a real terminal not_found state — never silently dropped."""
    from Email_validate_app.models import WarmupMessage

    message.check_attempts += 1
    if error_note:
        message.error = error_note

    if message.check_attempts < settings.WARMUP_MAX_CHECK_ATTEMPTS:
        backoff = settings.WARMUP_CHECK_BACKOFF_MINUTES
        delay_minutes = backoff[min(message.check_attempts - 1, len(backoff) - 1)]
        message.status      = 'sent'
        message.check_after = now() + timedelta(minutes=delay_minutes)
        message.save(update_fields=['check_attempts', 'status', 'check_after', 'error', 'updated_at'])
        warmup_check_one.apply_async(args=[message.id], eta=message.check_after)
        return {'status': 'not_found_retry', 'attempt': message.check_attempts}

    message.status           = 'completed'
    message.landing_location = 'not_found'
    message.checked_at       = now()
    message.save(update_fields=[
        'check_attempts', 'status', 'landing_location', 'checked_at', 'error', 'updated_at',
    ])
    return {'status': 'not_found_final'}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.warmup.warmup_recover_stuck',
    max_retries=0,
    base=LoggedTask,
)
def warmup_recover_stuck(self):
    """Mirrors so_recover_stuck_campaigns: reverts WarmupMessage rows stuck
    in 'sending'/'checking' past a staleness threshold (a worker crashed
    mid-task) so the next dispatch/check tick retries them."""
    from Email_validate_app.models import WarmupMessage

    cutoff = now() - timedelta(minutes=_STUCK_MINUTES)

    reverted_sending  = WarmupMessage.objects.filter(status='sending', updated_at__lt=cutoff).update(status='pending')
    reverted_checking = WarmupMessage.objects.filter(status='checking', updated_at__lt=cutoff).update(status='sent')

    return {'reverted_sending': reverted_sending, 'reverted_checking': reverted_checking}
