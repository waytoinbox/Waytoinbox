"""
Admin "Login as User" impersonation.

Pure session-key manipulation -- never reads, exposes, or modifies a
user's password. See Email_validate_app/utils.py::get_active_user for the
identity-resolution side of this (the impersonate_as_email branch), which
is what every other view/service in the app transparently picks up once
this sets that key. admin_required (views/admin/_base.py) needs no
changes: session['logged_in']/['is_admin'] are never touched here, so
they keep resolving to the true admin throughout.
"""
import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.timezone import now as tz_now
from django.views.decorators.http import require_POST

from Email_validate_app.models import ImpersonationLog, UserTable
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.views.auth import _get_client_ip

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
@require_POST
def admin_user_impersonate(request, uid):
    target = get_object_or_404(UserTable, pk=uid)
    admin = request._admin_user

    if target.pk == admin.pk:
        return json_error('You cannot impersonate yourself.')
    if target.is_admin:
        return json_error('Cannot impersonate another admin account.')

    # Keep impersonation and Main<->Sub Account switching mutually
    # exclusive so their UI (banner vs. profile-dropdown) never overlaps.
    request.session.pop('acting_as_email', None)
    request.session['impersonate_as_email'] = target.user_email
    # INF-05 precedent (login()/switch_account()): rotate the session key
    # before storing auth-adjacent data to prevent session fixation.
    request.session.cycle_key()
    request.session.modified = True

    ImpersonationLog.objects.create(
        admin=admin,
        target=target,
        target_email=target.user_email,
        ip_address=_get_client_ip(request),
        status='active',
    )
    audit(
        request, action='user.impersonate_start', module='users',
        target_type='user', target_id=uid, target_repr=target.user_email,
    )
    logger.info('Admin %s started impersonating %s', admin.user_email, target.user_email)

    return json_ok(data={'redirect': reverse('dashboard')})


@admin_required
@handle_admin_errors
@require_POST
def admin_exit_impersonation(request):
    admin = request._admin_user
    impersonated_email = request.session.pop('impersonate_as_email', None)
    request.session.cycle_key()
    request.session.modified = True

    if impersonated_email:
        open_log = ImpersonationLog.objects.filter(
            admin=admin, target_email=impersonated_email, status='active', ended_at__isnull=True,
        ).order_by('-started_at').first()
        if open_log:
            open_log.status = 'ended'
            open_log.ended_at = tz_now()
            open_log.save(update_fields=['status', 'ended_at'])

        audit(
            request, action='user.impersonate_end', module='users',
            target_type='user', target_repr=impersonated_email,
        )
        logger.info('Admin %s exited impersonation of %s', admin.user_email, impersonated_email)

    return json_ok(data={'redirect': reverse('admin_dashboard')})
