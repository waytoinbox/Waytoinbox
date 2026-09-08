import logging

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from Email_validate_app.models import FreeEmailSignupRequest
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import free_email_request_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_free_email_requests(request):
    page_obj, params, total = free_email_request_service.get_request_list(request.GET)
    return render(request, 'admin/free_email_requests/list.html', {
        'page': 'free_email_requests',
        'page_obj': page_obj,
        'params': params,
        'total': total,
    })


@admin_required
@handle_admin_errors
def admin_free_email_request_detail(request, rid):
    req = get_object_or_404(FreeEmailSignupRequest, pk=rid)
    return render(request, 'admin/free_email_requests/detail.html', {
        'page': 'free_email_requests',
        'req': req,
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_free_email_request_approve(request, rid):
    try:
        user, email_sent = free_email_request_service.approve_request(rid, request._admin_user, request)
    except FreeEmailSignupRequest.DoesNotExist:
        return json_error('Request not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='free_email_request.approve', module='free_email_requests',
        target_type='free_email_request', target_id=rid, target_repr=user.user_email,
        new_value={'created_user_id': user.pk, 'user_email': user.user_email},
    )
    message = f'Request approved. Account created for {user.user_email}.'
    if not email_sent:
        message += ' The password-setup email failed to send -- use "Forgot password" on the login page to resend it.'
    return json_ok(data={'email_sent': email_sent}, message=message)


@admin_required
@handle_admin_errors
@require_POST
def admin_free_email_request_reject(request, rid):
    reason = request.POST.get('reason', '').strip()
    if not reason:
        return json_error('A rejection reason is required.')

    try:
        req = free_email_request_service.reject_request(rid, request._admin_user, reason)
    except FreeEmailSignupRequest.DoesNotExist:
        return json_error('Request not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='free_email_request.reject', module='free_email_requests',
        target_type='free_email_request', target_id=rid, target_repr=req.email,
        new_value={'rejection_reason': reason},
    )
    return json_ok(message=f'Request for {req.email} rejected.')
