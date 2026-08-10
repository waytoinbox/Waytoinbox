import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from Email_validate_app.models import UserTable
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import user_service

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
    })


@admin_required
@handle_admin_errors
def admin_user_detail(request, uid):
    ctx = user_service.get_user_detail(uid)
    ctx['page'] = 'users'
    ctx['admin_pk'] = request._admin_user.pk
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
    delta = request.POST.get('delta', '').strip()
    try:
        credits_obj, old_balance, new_balance = user_service.adjust_credits(uid, delta)
    except UserTable.DoesNotExist:
        return json_error('User not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    user = credits_obj.user
    audit(
        request, action='user.credits', module='users',
        target_type='user', target_id=uid, target_repr=user.user_email,
        old_value={'current_credits': old_balance},
        new_value={'current_credits': new_balance, 'delta': int(delta)},
    )
    return json_ok(
        data={'new_balance': new_balance},
        message=f'Credits adjusted from {old_balance} → {new_balance}.',
    )


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
