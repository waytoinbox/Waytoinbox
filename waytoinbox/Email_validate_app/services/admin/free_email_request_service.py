"""Admin review of Free Email Signup Requests.

The one sanctioned exception to is_free_email_domain() -- self-service
signup (views/auth.py::signup) always blocks a free/public domain; this
module is the only place a UserTable row is created for such an address,
and only after an explicit admin decision. It deliberately never calls
is_free_email_domain() itself, and never touches CustomSignupForm (no
password is collected from the requester here).
"""
import logging
import secrets
from datetime import timedelta

from django.core.paginator import Paginator
from django.db import models, transaction, IntegrityError
from django.urls import reverse
from django.utils import timezone

from Email_validate_app.models import FreeEmailSignupRequest, UserTable
from Email_validate_app.services.mailer import (
    send_free_email_request_approved_email, send_free_email_request_rejected_email,
)

logger = logging.getLogger('Email_validate_app.services')

_PER_PAGE = 25


def get_request_list(request_get):
    """Return (page_obj, params, total) for the admin list page."""
    qs = FreeEmailSignupRequest.objects.select_related('reviewed_by', 'created_user').order_by('-created_at')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(models.Q(email__icontains=q) | models.Q(name__icontains=q))

    status = request_get.get('status', '')
    if status in ('pending', 'approved', 'rejected'):
        qs = qs.filter(status=status)

    page_num = request_get.get('page', 1)
    page_obj = Paginator(qs, _PER_PAGE).get_page(page_num)
    params = {'q': q, 'status': status}
    return page_obj, params, qs.count()


def get_request_detail(request_id):
    req = FreeEmailSignupRequest.objects.select_related('reviewed_by', 'created_user').get(pk=request_id)
    return {'req': req}


def approve_request(request_id, admin_user, http_request):
    """Create the UserTable account and mark the request approved.

    Both the account creation and the request-state change happen in one
    transaction -- if either fails, neither is left half-done. The
    password-setup email is sent AFTER that transaction commits (mirrors
    services/admin/user_service.py::trigger_password_reset's own
    established tolerance for mail flakiness -- a slow/failed send doesn't
    roll back state that is otherwise correct and durable; the caller
    reports the mail failure separately without reversing the approval).
    """
    with transaction.atomic():
        req = FreeEmailSignupRequest.objects.select_for_update().get(pk=request_id)
        if req.status != 'pending':
            raise ValueError('This request has already been processed.')

        if UserTable.objects.filter(user_email=req.email).exists():
            raise ValueError('An account with this email already exists. Request not approved.')

        user = UserTable(user_name=req.name, user_email=req.email)
        user.set_unusable_password()
        user.is_verified = True
        reset_token = secrets.token_urlsafe(20)
        user.reset_token = reset_token
        user.reset_token_expiry = timezone.now() + timedelta(hours=1)
        try:
            user.save()
        except IntegrityError:
            # Race: another request/signup claimed this email between the
            # exists() check above and this save().
            raise ValueError('An account with this email already exists. Request not approved.')

        req.status = 'approved'
        req.reviewed_by = admin_user
        req.reviewed_at = timezone.now()
        req.created_user = user
        req.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'created_user'])

    reset_link = http_request.build_absolute_uri(
        reverse('reset_password', kwargs={'token': reset_token})
    )
    try:
        send_free_email_request_approved_email(user.user_name, user.user_email, reset_link)
    except Exception as e:
        logger.error('Approval email failed for %s: %s', user.user_email, e)
        return user, False
    return user, True


def reject_request(request_id, admin_user, reason):
    with transaction.atomic():
        req = FreeEmailSignupRequest.objects.select_for_update().get(pk=request_id)
        if req.status != 'pending':
            raise ValueError('This request has already been processed.')

        req.status = 'rejected'
        req.rejection_reason = reason
        req.reviewed_by = admin_user
        req.reviewed_at = timezone.now()
        req.save(update_fields=['status', 'rejection_reason', 'reviewed_by', 'reviewed_at'])

    try:
        send_free_email_request_rejected_email(req.name, req.email, reason)
    except Exception as e:
        logger.error('Rejection email failed for %s: %s', req.email, e)
    return req
