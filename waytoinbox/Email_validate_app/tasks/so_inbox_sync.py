import logging

from celery import shared_task
from django.core.cache import cache

from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT = 600   # 10 min per account — Sent-folder reads + a 30-day first-sync backfill can run longer


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_inbox_sync.so_sync_all_inboxes',
    max_retries=0,
    base=LoggedTask,
)
def so_sync_all_inboxes(self):
    """Dispatcher — fans each connected account out to its own task instead of
    syncing them serially in one invocation, so one slow/hanging mailbox can't
    delay the rest. The per-account lock lives in so_sync_one_inbox itself,
    where the work actually happens, which is what makes fan-out safe."""
    from Email_validate_app.models import SOEmailAccount

    account_ids = list(SOEmailAccount.objects.filter(
        status='connected',
        deleted_at__isnull=True,
        imap_host__gt='',   # only accounts with IMAP configured
    ).values_list('id', flat=True))

    for account_id in account_ids:
        so_sync_one_inbox.delay(account_id)

    return {'dispatched': len(account_ids)}


@shared_task(
    bind=True,
    name='Email_validate_app.tasks.so_inbox_sync.so_sync_one_inbox',
    max_retries=0,
    soft_time_limit=240,
    time_limit=270,
    base=LoggedTask,
)
def so_sync_one_inbox(self, account_id):
    from Email_validate_app.models import SOEmailAccount
    from Email_validate_app.services.so_imap import sync_account_inbox

    lock_key = f'so_imap_sync:{account_id}'
    if not cache.add(lock_key, '1', timeout=_LOCK_TIMEOUT):
        return {'status': 'skipped_locked'}
    try:
        account = SOEmailAccount.objects.filter(id=account_id).first()
        if not account:
            return {'status': 'not_found'}
        sync_account_inbox(account)
        return {'status': 'ok'}
    except Exception as exc:
        logger.error('so_inbox_sync: error for account %s: %s', account_id, exc)
        return {'status': 'error', 'error': str(exc)}
    finally:
        cache.delete(lock_key)
