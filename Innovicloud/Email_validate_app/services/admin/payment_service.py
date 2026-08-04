"""Admin Payment & Subscription Service."""
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import models
from django.utils import timezone

from Email_validate_app.models import Payment, SubsPayment

logger = logging.getLogger('Email_validate_app.services')

_PER_PAGE = 25


def _safe_total(qs, field='amount'):
    total = Decimal('0')
    for val in qs.values_list(field, flat=True):
        try:
            total += Decimal(str(val or '0').replace(',', ''))
        except InvalidOperation:
            pass
    return float(total)


def get_payment_list(request_get):
    """Return (page_obj, params, total, total_revenue)."""
    qs = Payment.objects.select_related('user').order_by('-payment_time')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(payer_email__icontains=q)
            | models.Q(user__user_email__icontains=q)
            | models.Q(payment_id__icontains=q)
        )

    total = qs.count()
    total_revenue = round(_safe_total(Payment.objects.all()), 2)

    try:
        page_num = int(request_get.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, _PER_PAGE)
    page_obj = paginator.get_page(page_num)
    params = {'q': q}
    return page_obj, params, total, total_revenue


def get_subscription_list(request_get):
    """Return (page_obj, params, total)."""
    qs = SubsPayment.objects.select_related('user').order_by('-payment_time')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(payer_email__icontains=q)
            | models.Q(user__user_email__icontains=q)
            | models.Q(subs_plan__icontains=q)
        )

    status_filter = request_get.get('status', '')
    if status_filter:
        qs = qs.filter(plan_status=status_filter)

    total = qs.count()

    try:
        page_num = int(request_get.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, _PER_PAGE)
    page_obj = paginator.get_page(page_num)
    params = {'q': q, 'status': status_filter}
    return page_obj, params, total


def extend_subscription(sid, days):
    """Extend valid_time by `days` days from now (or from current expiry if future)."""
    try:
        days = int(days)
        if days <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Days must be a positive integer.')

    sub = SubsPayment.objects.get(pk=sid)
    base = (
        sub.valid_time
        if sub.valid_time and sub.valid_time > timezone.now()
        else timezone.now()
    )
    sub.valid_time = base + timedelta(days=days)
    sub.save(update_fields=['valid_time'])
    return sub
