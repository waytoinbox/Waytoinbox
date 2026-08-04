"""
Deprecated — dual-provider registration makes per-domain migration unnecessary.
Kept as a stub so any tasks already queued in Celery drain without crashing.
"""
import logging
from celery import shared_task
from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)


@shared_task(bind=True, base=LoggedTask, max_retries=0)
def migrate_domain_to_provider(self, domain_id):
    logger.info('migrate_domain_to_provider: task is deprecated, skipping domain_id=%s', domain_id)
