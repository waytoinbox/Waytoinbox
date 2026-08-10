import logging
from django.shortcuts import render
from django.contrib.auth.hashers import check_password, make_password

from Email_validate_app.views.admin._base import (
    admin_required, handle_admin_errors, audit, json_ok, json_error,
)

logger = logging.getLogger('Email_validate_app.views')

ALLOWED_FIELDS = {'user_name', 'company', 'role', 'timezone', 'website'}


@admin_required
@handle_admin_errors
def admin_profile(request):
    admin_user = request._admin_user

    if request.method == 'POST':
        action = request.POST.get('action', 'profile')
        if action == 'password':
            return _change_password(request, admin_user)
        return _update_profile(request, admin_user)

    return render(request, 'admin/profile/index.html', {
        'page': 'profile',
        'admin_user': admin_user,
    })


def _update_profile(request, admin_user):
    updated = {}
    for field in ALLOWED_FIELDS:
        if field in request.POST:
            val = request.POST[field].strip()
            setattr(admin_user, field, val)
            updated[field] = val
    admin_user.save(update_fields=list(updated.keys()))
    audit(request, 'profile.update', 'profile', new_value=updated)
    return json_ok(message='Profile updated.')


def _change_password(request, admin_user):
    current = request.POST.get('current_password', '')
    new_pw = request.POST.get('new_password', '')
    confirm = request.POST.get('confirm_password', '')

    if not check_password(current, admin_user.password):
        return json_error('Current password is incorrect.')
    if len(new_pw) < 8:
        return json_error('New password must be at least 8 characters.')
    if new_pw != confirm:
        return json_error('Passwords do not match.')

    admin_user.password = make_password(new_pw)
    admin_user.save(update_fields=['password'])
    audit(request, 'profile.password_change', 'profile')
    request.session.cycle_key()  # INF-06
    return json_ok(message='Password changed successfully.')
