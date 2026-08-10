"""
Admin User Management Service.
All user-management business logic lives here; views stay thin.
"""
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import models
from django.db.models import IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.template.loader import render_to_string
from django.utils import timezone

from Email_validate_app.models import (
    Campaign, CurrentCredits, ListFiles, LoginActivity,
    Payment, SubsPayment, UserTable,
)

logger = logging.getLogger('Email_validate_app.services')

_PER_PAGE = 25


# ── Helpers ───────────────────────────────────────────────────────────────────

def _credits_subquery():
    return Coalesce(
        Subquery(
            CurrentCredits.objects.filter(user=OuterRef('pk'))
            .values('vc_current_credits')[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )


# ── User List ─────────────────────────────────────────────────────────────────

def get_user_list(request_get):
    """Return (page_obj, filter_params) for the user list page."""
    qs = UserTable.objects.annotate(
        credit_balance=_credits_subquery()
    ).order_by('-created_date')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(user_email__icontains=q)
            | models.Q(user_name__icontains=q)
            | models.Q(company__icontains=q)
        )

    status_filter = request_get.get('status', '')
    if status_filter == 'active':
        qs = qs.filter(is_active=True)
    elif status_filter == 'inactive':
        qs = qs.filter(is_active=False)

    admin_filter = request_get.get('admin', '')
    if admin_filter == '1':
        qs = qs.filter(is_admin=True)

    verified_filter = request_get.get('verified', '')
    if verified_filter == '1':
        qs = qs.filter(is_verified=True)
    elif verified_filter == '0':
        qs = qs.filter(is_verified=False)

    total = qs.count()

    try:
        page_num = int(request_get.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, _PER_PAGE)
    page_obj = paginator.get_page(page_num)

    params = {
        'q': q,
        'status': status_filter,
        'admin': admin_filter,
        'verified': verified_filter,
    }
    return page_obj, params, total


# ── User Detail ───────────────────────────────────────────────────────────────

def get_user_detail(uid):
    """
    Return full context dict for the user detail page.
    Raises UserTable.DoesNotExist if uid is not found.
    """
    user = UserTable.objects.get(pk=uid)
    credits = CurrentCredits.objects.filter(user=user).first()
    subscription = SubsPayment.objects.filter(user=user).order_by('-payment_time').first()
    login_history = (
        LoginActivity.objects.filter(user=user)
        .only('login_at', 'logout_at', 'ip_address', 'browser', 'os', 'device', 'status')
        .order_by('-login_at')[:10]
    )
    recent_validations = (
        ListFiles.objects.filter(user=user)
        .only('file_id', 'file_name', 'job_status', 'total_count', 'valid_count', 'insert_date')
        .order_by('-insert_date')[:5]
    )
    recent_campaigns = (
        Campaign.objects.filter(user=user)
        .only('id', 'campaign_name', 'status', 'total_recipients', 'created_at')
        .order_by('-created_at')[:5]
    )
    recent_payments = (
        Payment.objects.filter(user=user)
        .only('id', 'amount', 'currency', 'credits', 'payment_time')
        .order_by('-payment_time')[:5]
    )
    return {
        'target_user': user,
        'credits': credits,
        'subscription': subscription,
        'login_history': login_history,
        'recent_validations': recent_validations,
        'recent_campaigns': recent_campaigns,
        'recent_payments': recent_payments,
    }


# ── Mutations ─────────────────────────────────────────────────────────────────

def toggle_user_active(uid):
    """Toggle is_active. Returns (user, new_active_bool)."""
    user = UserTable.objects.get(pk=uid)
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    return user, user.is_active


def verify_user(uid):
    """Set is_verified=True. Raises ValueError if already verified."""
    user = UserTable.objects.get(pk=uid)
    if user.is_verified:
        raise ValueError('User is already verified.')
    user.is_verified = True
    user.save(update_fields=['is_verified'])
    return user


def toggle_admin(uid, requesting_admin):
    """
    Toggle is_admin. Cannot revoke own admin.
    Returns (user, new_is_admin_bool).
    """
    user = UserTable.objects.get(pk=uid)
    if user.pk == requesting_admin.pk:
        raise ValueError('You cannot modify your own admin status.')
    user.is_admin = not user.is_admin
    user.save(update_fields=['is_admin'])
    return user, user.is_admin


def adjust_credits(uid, delta):
    """
    Adjust current_credits by delta (signed int).
    Creates a CurrentCredits row if none exists.
    Returns (credits_obj, old_balance, new_balance).
    """
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise ValueError('Delta must be an integer.')
    if delta == 0:
        raise ValueError('Delta cannot be zero.')

    user = UserTable.objects.get(pk=uid)
    credits_obj, _ = CurrentCredits.objects.get_or_create(
        user=user,
        defaults={
            'vc_total_credits': 0, 'vc_used_credits': 0, 'vc_current_credits': 0,
            'ac_total_credits': 0, 'ac_used_credits': 0, 'ac_current_credits': 0,
        },
    )
    old_balance = credits_obj.vc_current_credits or 0
    new_balance = old_balance + delta
    if new_balance < 0:
        raise ValueError(
            f'Cannot reduce balance below 0. Current: {old_balance}, delta: {delta}.'
        )
    credits_obj.vc_current_credits = new_balance
    credits_obj.save(update_fields=['vc_current_credits'])
    return credits_obj, old_balance, new_balance


def trigger_password_reset(uid, request):
    """
    Generate a reset token and send the password reset email.
    Reuses the same token/email flow as the forgot_password view.
    Returns user.
    """
    from django.urls import reverse

    user = UserTable.objects.get(pk=uid)
    reset_token = secrets.token_urlsafe(20)
    user.reset_token = reset_token
    user.reset_token_expiry = timezone.now() + timedelta(hours=1)
    user.save(update_fields=['reset_token', 'reset_token_expiry'])

    reset_link = request.build_absolute_uri(
        reverse('reset_password', kwargs={'token': reset_token})
    )
    email_content = render_to_string(
        'i_password_reset_email.html', {'reset_link': reset_link}
    )
    send_mail(
        subject='Password Reset Request',
        message='',
        from_email=settings.EMAIL_HOST_USER,
        recipient_list=[user.user_email],
        html_message=email_content,
        fail_silently=False,
    )
    return user


def edit_user_profile(uid, data):
    """
    Update editable profile fields from a POST dict.
    Returns saved user.
    """
    user = UserTable.objects.get(pk=uid)

    name = data.get('user_name', '').strip()
    if not name:
        raise ValueError('Name is required.')
    user.user_name = name
    user.company = data.get('company', '').strip() or None
    user.role = data.get('role', '').strip() or None
    user.timezone = data.get('timezone', '').strip() or None

    website = data.get('website', '').strip()
    user.website = website or None

    user.save()
    return user


def soft_delete_user(uid, requesting_admin):
    """
    Soft-delete: deactivates the user. Cannot delete yourself.
    Returns deactivated user.
    """
    user = UserTable.objects.get(pk=uid)
    if user.pk == requesting_admin.pk:
        raise ValueError('You cannot delete your own account.')
    user.is_active = False
    user.save(update_fields=['is_active'])
    return user
