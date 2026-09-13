"""Shared test-only helper for funding a user through the real new-system
purchase-grant path (ServiceCreditLot via grant_credit_lot()), so the
credit/entitlement test suite has ONE place that knows how to do this
instead of duplicating lot-creation logic across files.

Deliberately NOT a production helper -- lives in tests/, imports the real
grant_credit_lot() so every test using it exercises the actual grant code,
never a hand-rolled substitute or a direct ServiceCreditLot.objects.create()
that could drift from what a real purchase actually writes.
"""
from datetime import timedelta

from django.utils.timezone import now

from Email_validate_app.models import ServiceCreditLot
from Email_validate_app.services.credit_manager import grant_credit_lot


def grant_lot(user_id, service, amount=1, hours_ago_purchased=0):
    """Grant `amount` new-system credits for `service` via the real
    grant_credit_lot() path.

    `hours_ago_purchased` back-dates `purchased_at` so a test can simulate
    a lot already past its nominal 30*24h expiry (for any service other
    than email_validation, which never expires regardless -- see
    NON_EXPIRING_LOT_SERVICES in credit_manager.py). 0 (the default) is a
    fresh, currently-active lot.
    """
    purchased_at = now() - timedelta(hours=hours_ago_purchased)
    return grant_credit_lot(
        user_id, service, amount,
        source=ServiceCreditLot.SOURCE_SERVICE_CHECKOUT, purchased_at=purchased_at,
        ref_type='service_purchase', ref_id='test',
    )


# Backward-compatible alias -- test_phase4_entitlements.py's existing local
# helper of the same shape/signature is superseded by this one.
make_lot = grant_lot
