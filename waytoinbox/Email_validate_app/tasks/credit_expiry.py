"""
Phase 4: Celery expiry sweeps.

Two independent jobs:

  expire_credit_lots()        -- finalizes ServiceCreditLot rows past
                                  expires_at, cascading to end any active
                                  lot-funded entitlement. Skips
                                  NON_EXPIRING_LOT_SERVICES (email_validation)
                                  entirely -- see credit_manager.py.
  expire_trial_entitlements() -- ends trial-funded entitlements once
                                  UserTable.trial_ends_at has passed.
                                  Deliberately independent of
                                  trial_expiry_notification_job's own
                                  trial_expiry_notified_at guard: that flag
                                  fires once ever per user, and would
                                  silently stop covering a second
                                  trial-funded entitlement created after a
                                  trial reset/extension. See its own
                                  docstring below.

Both are idempotent and safe under concurrent/overlapping execution -- see
each function's own docstring. Neither touches
deduct_service_credits()/refund_service_credits()/grant_credit_lot(), trial
usage counters (ServiceTrial.used), or TrialUsageLog.
"""

import logging

from celery import shared_task
from django.db import transaction
from django.utils.timezone import now

from Email_validate_app.models import (
    ServiceCreditLot, ServiceEntitlement, CreditAuditLog, UserTable,
)
from Email_validate_app.services.credit_manager import SERVICE_LABELS, NON_EXPIRING_LOT_SERVICES
from Email_validate_app.services.entitlement_manager import (
    end_entitlement, EntitlementIntegrityError,
)
from Email_validate_app.services.mailer import send_job_failure_alert
from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def _report_entitlement_integrity_error(task_name, exc, **context):
    """One bad entitlement must not stop the rest of a sweep from
    processing, but it must never be silently swallowed either: log loudly
    AND send the same job-failure alert a whole-task failure would trigger,
    so an active entitlement left standing against an already-expired
    lot/trial cannot go unnoticed."""
    logger.error('%s: entitlement integrity failure | %s | %s', task_name, exc, context)
    try:
        send_job_failure_alert(task_name, exc, context=context)
    except Exception:
        logger.warning('%s: send_job_failure_alert itself failed', task_name)


@shared_task(
    name='Email_validate_app.tasks.credit_expiry.expire_credit_lots',
    base=LoggedTask,
)
def expire_credit_lots():
    """Finalize every ServiceCreditLot whose expires_at has passed. Runs for
    every service, not just the entitlement-eligible ones -- a plain
    email_validation/email_marketing/header_analysis lot is still correctly
    finalized, it just never reaches the entitlement cascade below (nothing
    ever references it).

    Idempotent: _expire_one_lot() re-checks status/expires_at under the
    lot's own row lock, so a retried or overlapping run touching the same
    lot is a no-op the second time. Only quantity_remaining is ever moved
    into quantity_expired -- quantity_used (already-spent credit) is never
    touched, and ServiceCredit.balance/CurrentCredits are never read or
    written here at all.

    Excludes NON_EXPIRING_LOT_SERVICES (email_validation) entirely -- those
    lots keep their purchased_at/expires_at metadata for audit/history, but
    are never finalized/expired here, so their remaining credit stays
    spendable indefinitely (see deduct_service_credits())."""
    lot_ids = list(ServiceCreditLot.objects.filter(
        status=ServiceCreditLot.STATUS_ACTIVE, expires_at__lte=now(),
    ).exclude(service__in=NON_EXPIRING_LOT_SERVICES).values_list('id', flat=True)[:BATCH_SIZE])

    for lot_id in lot_ids:
        _expire_one_lot(lot_id)
    return {'lots_processed': len(lot_ids)}


def _expire_one_lot(lot_id):
    with transaction.atomic():
        lot = ServiceCreditLot.objects.select_for_update().get(pk=lot_id)
        if lot.status != ServiceCreditLot.STATUS_ACTIVE:
            return  # already processed by an earlier/overlapping run
        if lot.expires_at is None:
            # Defense-in-depth: a permanent admin-granted lot (expires_at
            # IS NULL -- see credit_manager.grant_admin_credit_lot) must
            # never be finalized here. expire_credit_lots()'s own selection
            # query (expires_at__lte=now()) already excludes NULL rows at
            # the SQL level, so this only matters for a lot ID reaching
            # this function by any other path (e.g. direct/manual
            # invocation) -- same defense-in-depth role the
            # NON_EXPIRING_LOT_SERVICES check just below already plays.
            return
        if lot.expires_at > now():
            return  # already processed by an earlier/overlapping run
        if lot.service in NON_EXPIRING_LOT_SERVICES:
            # Defense-in-depth: expire_credit_lots()'s own selection query
            # already excludes these, but a lot ID reaching this function by
            # any other path (e.g. direct/manual invocation) must never
            # finalize a service whose remaining credit must stay usable
            # indefinitely.
            return

        remaining = lot.quantity_remaining
        if remaining > 0:
            lot.quantity_remaining = 0
            lot.quantity_expired   = (lot.quantity_expired or 0) + remaining
            CreditAuditLog.objects.create(
                user_id=lot.user_id, credit_type=lot.service, entry_type='expired',
                amount=-remaining, balance_before=remaining, balance_after=0,
                ref_type='lot_expiry', ref_id=str(lot.id), lot=lot, service=lot.service,
                description=f"Lot #{lot.id} expired: {remaining} unused "
                            f"{SERVICE_LABELS.get(lot.service, lot.service)} credits forfeited",
            )
        lot.status     = ServiceCreditLot.STATUS_EXPIRED
        lot.expired_at = now()
        lot.save(update_fields=['quantity_remaining', 'quantity_expired',
                                 'status', 'expired_at', 'updated_at'])

        ent_ids = list(ServiceEntitlement.objects.filter(
            lot_id=lot_id, status=ServiceEntitlement.STATUS_ACTIVE,
        ).values_list('id', flat=True))

    # Each entitlement ends in its OWN transaction, outside the lot's own
    # atomic() block above -- a bad entitlement (EntitlementIntegrityError)
    # rolls back only its own ending, never the lot finalization that
    # already committed, and never blocks this lot's OTHER entitlements.
    for ent_id in ent_ids:
        try:
            end_entitlement(ent_id, reason=ServiceEntitlement.END_REASON_EXPIRED)
        except EntitlementIntegrityError as exc:
            _report_entitlement_integrity_error(
                'Email_validate_app.tasks.credit_expiry.expire_credit_lots',
                exc, lot_id=lot_id, entitlement_id=ent_id)


@shared_task(
    name='Email_validate_app.tasks.credit_expiry.expire_trial_entitlements',
    base=LoggedTask,
)
def expire_trial_entitlements():
    """End every active trial-funded entitlement whose user's trial has
    already ended. Deliberately independent of
    trial_expiry_notification_job / trial_expiry_notified_at: that flag is
    designed to fire once ever per user, so coupling entitlement-ending to
    it would permanently miss any trial-funded entitlement created during a
    second trial window (e.g. after a manual trial reset/extension) whose
    own expiry comes after the first notification already fired.
    ServiceTrial.used and TrialUsageLog are never read or written here --
    only UserTable.trial_ends_at (read) and ServiceEntitlement/target
    resource rows (written).

    Idempotent by construction: this re-derives its answer fresh from
    current state on every run (trial_ends_at <= now(), status='active'),
    with no one-time flag anywhere -- an already-ended entitlement is a
    cheap no-op via end_entitlement()'s own status re-check."""
    user_ids = list(UserTable.objects.filter(
        trial_ends_at__isnull=False, trial_ends_at__lte=now(),
    ).values_list('id', flat=True))

    ended_total = 0
    for user_id in user_ids:
        ended_total += _expire_trial_entitlements_for_user(user_id)
    return {'users_checked': len(user_ids), 'entitlements_ended': ended_total}


def _expire_trial_entitlements_for_user(user_id):
    ent_ids = list(ServiceEntitlement.objects.filter(
        user_id=user_id, funding_source=ServiceEntitlement.FUNDING_TRIAL,
        status=ServiceEntitlement.STATUS_ACTIVE,
    ).values_list('id', flat=True))

    ended = 0
    for ent_id in ent_ids:
        try:
            end_entitlement(ent_id, reason=ServiceEntitlement.END_REASON_TRIAL_ENDED)
            ended += 1
        except EntitlementIntegrityError as exc:
            _report_entitlement_integrity_error(
                'Email_validate_app.tasks.credit_expiry.expire_trial_entitlements',
                exc, user_id=user_id, entitlement_id=ent_id)
    return ended
