import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from Email_validate_app.models import SERVICE_CHOICES, SERVICE_KEYS, UserTable
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import user_service
from Email_validate_app.services.credit_manager import SERVICE_LABELS, grant_admin_credit_lot

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_users(request):
    page_obj, params, total = user_service.get_user_list(request.GET)
    return render(request, 'admin/users/list.html', {
        'page': 'users',
        'page_obj': page_obj,
        'params': params,
        'total': total,
        'admin_pk': request._admin_user.pk,
    })


@admin_required
@handle_admin_errors
def admin_user_detail(request, uid):
    ctx = user_service.get_user_detail(uid)
    ctx['page'] = 'users'
    ctx['admin_pk'] = request._admin_user.pk
    ctx['service_choices'] = SERVICE_CHOICES
    return render(request, 'admin/users/detail.html', ctx)


@admin_required
@handle_admin_errors
def admin_user_edit(request, uid):
    user = get_object_or_404(UserTable, pk=uid)
    if request.method == 'POST':
        try:
            updated = user_service.edit_user_profile(uid, request.POST)
            audit(
                request, action='user.edit', module='users',
                target_type='user', target_id=uid, target_repr=updated.user_email,
                old_value={'user_name': user.user_name, 'company': user.company},
                new_value={'user_name': updated.user_name, 'company': updated.company},
            )
            return json_ok(message='Profile updated successfully.')
        except ValueError as exc:
            return json_error(str(exc))
    return render(request, 'admin/users/edit.html', {
        'page': 'users',
        'target_user': user,
        'admin_pk': request._admin_user.pk,
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_user_toggle(request, uid):
    try:
        user, new_active = user_service.toggle_user_active(uid)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)

    label = 'activated' if new_active else 'deactivated'
    audit(
        request, action='user.toggle', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
        old_value={'is_active': not new_active},
        new_value={'is_active': new_active},
    )
    badge = (
        '<span class="badge badge-success"><span class="badge-dot"></span>Active</span>'
        if new_active else
        '<span class="badge badge-grey"><span class="badge-dot"></span>Inactive</span>'
    )
    return json_ok(data={'badge_html': badge, 'is_active': new_active},
                   message=f'User {label}.')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_verify(request, uid):
    try:
        user = user_service.verify_user(uid)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='user.verify', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
        new_value={'is_verified': True},
    )
    return json_ok(message='User marked as verified.')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_grant_admin(request, uid):
    try:
        user, new_is_admin = user_service.toggle_admin(uid, request._admin_user)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    label = 'granted' if new_is_admin else 'revoked'
    audit(
        request, action='user.grant_admin', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
        old_value={'is_admin': not new_is_admin},
        new_value={'is_admin': new_is_admin},
    )
    return json_ok(data={'is_admin': new_is_admin},
                   message=f'Admin access {label} for {user.user_email}.')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_credits(request, uid):
    """Admin "Grant Credits" — creates one ServiceCreditLot per selected
    service via credit_manager.grant_admin_credit_lot(), the Phase 3 lot
    architecture live purchases already use. Fully isolated from
    grant_credit_lot() itself (never called here) and from the legacy
    CurrentCredits.vc_current_credits pool this endpoint used to adjust
    (see the old docstring this replaces / credit_manager.
    LEGACY_BALANCES_SPENDABLE, still False and untouched) -- that old,
    no-longer-usable adjustment path is gone, not merely disabled.

    Expected JSON body:
        {
          "expiry_mode": "permanent" | "30_days",
          "services": [{"service": "<key>", "amount": <int>}, ...]
        }
    Only services explicitly present in the payload are granted -- there is
    no "select all" default, matching "only checked services should be
    submitted" (the client only ever includes checked rows; the server
    independently re-validates every entry regardless of what the client
    claims to have checked).
    """
    target_user = get_object_or_404(UserTable, pk=uid)

    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return json_error('Invalid request body.')
    if not isinstance(data, dict):
        return json_error('Invalid request body.')

    expiry_mode = data.get('expiry_mode')
    if expiry_mode not in ('permanent', '30_days'):
        return json_error('Choose an expiry option: Permanent or 30 Days.')
    permanent = (expiry_mode == 'permanent')

    raw_services = data.get('services')
    if not isinstance(raw_services, list) or not raw_services:
        return json_error('Select at least one service to grant credits to.')

    # Validate every entry fully before writing anything -- a bad entry
    # anywhere in the payload must reject the whole request, never grant
    # a partial subset.
    seen = set()
    cleaned = []
    for entry in raw_services:
        if not isinstance(entry, dict):
            return json_error('Malformed service entry.')
        service = entry.get('service')
        if service not in SERVICE_KEYS:
            return json_error(f'Unknown service: {service!r}.')
        if service in seen:
            return json_error(f'{SERVICE_LABELS[service]} was submitted more than once.')
        seen.add(service)
        try:
            amount = int(entry.get('amount'))
        except (TypeError, ValueError):
            return json_error(f'{SERVICE_LABELS[service]}: enter a valid credit amount.')
        if amount <= 0:
            return json_error(f'{SERVICE_LABELS[service]}: amount must be a positive number.')
        cleaned.append((service, amount))

    # Atomic: if any single grant in this batch fails, none of them commit.
    # grant_admin_credit_lot() already wraps its own single-lot write in its
    # own transaction.atomic(); nesting those inside this outer atomic()
    # block (Django collapses nested atomic() into savepoints on the same
    # connection) is what makes a multi-service submission all-or-nothing.
    granted = []
    with transaction.atomic():
        for service, amount in cleaned:
            lot = grant_admin_credit_lot(
                target_user.id, service, amount,
                permanent=permanent, granted_by=request._admin_user,
            )
            granted.append((service, amount, lot))

    audit(
        request, action='user.grant_credits', module='users',
        target_type='user', target_id=target_user.id, target_repr=target_user.user_email,
        new_value={
            'expiry_mode': expiry_mode,
            'grants': [{'service': s, 'amount': a} for s, a, _ in granted],
        },
    )

    summary = ', '.join(f'{a} {SERVICE_LABELS[s]}' for s, a, _ in granted)
    return json_ok(message=f'Granted {summary} to {target_user.user_email}.')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_reset_password(request, uid):
    try:
        user = user_service.trigger_password_reset(uid, request)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)
    except Exception as exc:
        logger.error('Password reset email failed for uid=%s: %s', uid, exc)
        return json_error('Failed to send reset email. Check mail settings.')

    audit(
        request, action='user.reset_password', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
    )
    return json_ok(message=f'Password reset email sent to {user.user_email}.')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_delete(request, uid):
    try:
        user = user_service.soft_delete_user(uid, request._admin_user)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='user.delete', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
        new_value={'is_active': False},
    )
    return json_ok(
        data={'redirect': '/wti-admin/users/'},
        message=f'User {user.user_email} deactivated.',
    )
