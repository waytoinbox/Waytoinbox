"""
Phase 4: ServiceEntitlement lifecycle.

This module owns the only code allowed to create or end a ServiceEntitlement
row, and the only code allowed to flip a managed resource's
`entitlement_status` between 'active' and 'suspended'. Centralising both
here is what makes "ending an entitlement suspends its resource" a single
enforced invariant instead of something every call site has to remember to
do correctly.

Deliberately does NOT touch deduct_service_credits()/refund_service_credits()/
grant_credit_lot() in services/credit_manager.py — Phase 3's credit
deduction/refund/FEFO logic is unmodified and unread beyond the audit rows
it already writes (CreditAuditLog/TrialUsageLog), which this module only
ever reads, never writes to directly.
"""

import logging

from django.db import transaction
from django.utils.timezone import now

from Email_validate_app.models import (
    ServiceEntitlement, ServiceCreditLot, CreditAuditLog, TrialUsageLog,
    UserTable, BlocklistMonitor, DomainBlocklist, Reputation, SOEmailAccount,
    SERVICE_KEYS,
)

logger = logging.getLogger(__name__)

# entitlement target field name -> the model it points at. Reused both to
# build an entitlement (create_entitlement) and to tear one down
# (end_entitlement) without duplicating this mapping in two places.
TARGET_FIELDS = {
    'so_account':     SOEmailAccount,
    'reputation':     Reputation,
    'ip_monitor':     BlocklistMonitor,
    'domain_monitor': DomainBlocklist,
}
_MODEL_TO_TARGET_FIELD = {model: field for field, model in TARGET_FIELDS.items()}


class FundingResolutionError(RuntimeError):
    """resolve_funding() could not identify exactly one funding source for a
    spend_id. Always raised INSIDE the same transaction as the deduction
    being resolved, so raising here rolls that deduction back too — nobody
    is ever charged for a spend whose entitlement bookkeeping failed."""


class EntitlementIntegrityError(RuntimeError):
    """end_entitlement() found that its target resource does not exist under
    the entitlement's own user_id. Raised rather than silently suspending
    nothing (or someone else's resource) — callers must not swallow this."""


def resolve_funding(user_id, service, spend_id):
    """Identify exactly which single source funded a count=1 deduction.

    Proven safe for every current count=1 call site: deduct_service_credits()
    allocates count strictly sequentially (trial -> lots -> wallet -> legacy),
    and each stage only proceeds while a `remainder` is still positive. For
    count=1, the first stage that contributes anything at all immediately
    drives remainder to 0, so every later stage contributes exactly 0 — at
    most one debit row is ever written, across all four sources combined.

    The strict multi-row/ambiguity checks below are kept anyway as
    defense-in-depth against any future change to deduct_service_credits()
    that might alter this invariant — not because they are expected to fire
    today.

    A spend_id belonging to a different user/service can never reach the
    ambiguity checks: every query here is filtered by the exact user_id/
    service the caller passed, so a mismatched row is structurally excluded
    and simply shows up as "no debit rows found" below.

    Returns (funding_source, lot_or_None). Raises FundingResolutionError on
    any ambiguity, missing data, or unrecognized shape — never guesses.
    """
    trial_debits = list(TrialUsageLog.objects.filter(
        user_id=user_id, service=service, spend_id=spend_id, entry_type='debit',
    ))
    lot_debits = list(CreditAuditLog.objects.filter(
        user_id=user_id, service=service, spend_id=spend_id,
        entry_type='debit', lot__isnull=False,
    ))
    other_debits = list(CreditAuditLog.objects.filter(
        user_id=user_id, service=service, spend_id=spend_id,
        entry_type='debit', lot__isnull=True,
    ))

    kinds_present = sum(bool(x) for x in (trial_debits, lot_debits, other_debits))
    if kinds_present == 0:
        raise FundingResolutionError(
            f"No debit rows found for user={user_id} service={service!r} "
            f"spend_id={spend_id!r}")
    if kinds_present > 1:
        raise FundingResolutionError(
            f"Ambiguous funding for user={user_id} service={service!r} "
            f"spend_id={spend_id!r}: matched more than one source type")

    if trial_debits:
        if len(trial_debits) > 1:
            raise FundingResolutionError(
                f"Multiple trial debit rows for spend_id={spend_id!r}")
        return ServiceEntitlement.FUNDING_TRIAL, None

    if lot_debits:
        if len(lot_debits) > 1:
            raise FundingResolutionError(
                f"Multiple lot debit rows for spend_id={spend_id!r}")
        debit = lot_debits[0]
        try:
            lot = ServiceCreditLot.objects.get(pk=debit.lot_id)
        except ServiceCreditLot.DoesNotExist:
            raise FundingResolutionError(
                f"Lot debit for spend_id={spend_id!r} references missing "
                f"lot {debit.lot_id}")
        return ServiceEntitlement.FUNDING_LOT, lot

    if len(other_debits) > 1:
        raise FundingResolutionError(
            f"Multiple non-lot debit rows for spend_id={spend_id!r}")
    debit = other_debits[0]
    if debit.credit_type == service:
        return ServiceEntitlement.FUNDING_WALLET, None
    if debit.credit_type in ('vc', 'ac', 'cc'):
        return ServiceEntitlement.FUNDING_LEGACY_POOL, None
    raise FundingResolutionError(
        f"Unrecognized credit_type={debit.credit_type!r} for "
        f"spend_id={spend_id!r}")


def create_entitlement(user_id, service, target_obj, funding_source, lot=None,
                       spend_id=''):
    """Create a new, active ServiceEntitlement for a freshly funded resource
    (a brand-new resource, or a Reactivate). `target_obj` must be the actual
    row this entitlement covers — its model class determines which of the
    four target FKs is set.

    Never called for a resource that already has an active entitlement in
    valid usage (a new resource has none yet; Reactivate only runs after
    confirming under lock that the previous one has already ended). If that
    invariant were ever violated, the `active_key` unique constraint rejects
    the conflicting INSERT at the database level rather than silently
    allowing two active entitlements for the same item — so this function
    deliberately does not pre-check for an existing active entitlement
    itself (that would be an extra query and an extra, unnecessary lock).
    """
    if service not in SERVICE_KEYS:
        raise ValueError(f"Unknown service: {service!r}")
    field = _MODEL_TO_TARGET_FIELD.get(type(target_obj))
    if field is None:
        raise ValueError(f"{type(target_obj)!r} is not a valid entitlement target")
    assert target_obj.user_id == user_id, (
        f"create_entitlement: target {type(target_obj).__name__}#{target_obj.pk} "
        f"belongs to user {target_obj.user_id}, not {user_id}")

    if funding_source == ServiceEntitlement.FUNDING_LOT:
        if lot is None:
            raise ValueError("funding_source='lot' requires `lot`")
        expires_at = lot.expires_at
    elif funding_source == ServiceEntitlement.FUNDING_TRIAL:
        expires_at = UserTable.objects.filter(pk=user_id).values_list(
            'trial_ends_at', flat=True).first()
    else:
        expires_at = None   # wallet / legacy_pool / grandfathered: permanent

    return ServiceEntitlement.objects.create(
        user_id=user_id, service=service,
        **{field: target_obj},
        funding_source=funding_source, lot=lot,
        status=ServiceEntitlement.STATUS_ACTIVE,
        activated_at=now(), expires_at=expires_at,
        spend_id=spend_id,
        active_key=f'{service}:{field}:{target_obj.pk}',
    )


def end_entitlement(entitlement_id, reason):
    """End one ServiceEntitlement and suspend its target resource, as a
    single ownership-checked operation. Idempotent: a no-op if the
    entitlement is already ended (safe under concurrent/duplicate calls,
    e.g. an overlapping sweep run).

    Raises EntitlementIntegrityError if the target resource cannot be found
    under the entitlement's own user_id — this must never be swallowed
    silently by a caller, since it means an entitlement is ending without
    its resource actually being suspended.
    """
    with transaction.atomic():
        ent = ServiceEntitlement.objects.select_for_update().get(pk=entitlement_id)
        if ent.status != ServiceEntitlement.STATUS_ACTIVE:
            return  # already ended — idempotent no-op

        ent.status     = ServiceEntitlement.STATUS_ENDED
        ent.ended_at   = now()
        ent.end_reason = reason
        ent.active_key = None
        ent.save(update_fields=['status', 'ended_at', 'end_reason', 'active_key', 'updated_at'])

        field, model_cls = next(
            (f, m) for f, m in TARGET_FIELDS.items() if getattr(ent, f'{f}_id'))
        target_pk = getattr(ent, f'{field}_id')

        # The ownership check and the mutation are the SAME statement, so
        # there is no window between "check ownership" and "do the update":
        # a target that doesn't exist under this entitlement's user_id
        # simply matches zero rows.
        updated = model_cls.objects.filter(
            pk=target_pk, user_id=ent.user_id,
        ).update(entitlement_status='suspended')
        if updated == 0:
            raise EntitlementIntegrityError(
                f"Entitlement {ent.pk} target {model_cls.__name__}#{target_pk} "
                f"was not found under user {ent.user_id} — refusing to suspend")
