"""
Admin Dashboard Service.

All query and aggregation logic for the dashboard page lives here.
Views call these functions and pass the results directly to templates.
"""
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from Email_validate_app.models import (
    UserTable, Campaign, ListFiles, CampaignStats,
    Payment, SubsPayment, CurrentCredits, UsedCredits,
    AdminActivity,
)

logger = logging.getLogger('Email_validate_app.services')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_sum_amounts(queryset, field='amount'):
    """Sum CharField amounts safely; returns float."""
    total = Decimal('0')
    for val in queryset.values_list(field, flat=True):
        try:
            total += Decimal(str(val or '0').replace(',', ''))
        except InvalidOperation:
            pass
    return float(total)


def _date_series(days):
    """Return list of date strings for the last `days` days (oldest first)."""
    today = timezone.now().date()
    return [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def _fill_series(queryset_values, date_series):
    """
    Convert a list of {'date': date, 'count': n} dicts to a flat list
    aligned with `date_series`, filling gaps with 0.
    """
    lookup = {str(r['date']): r['count'] for r in queryset_values}
    return [lookup.get(d, 0) for d in date_series]


# ── Stat Cards ────────────────────────────────────────────────────────────────

def get_stat_cards():
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)

    total_users  = UserTable.objects.count()
    active_users = UserTable.objects.filter(is_active=True).count()
    new_today    = UserTable.objects.filter(created_date__date=today).count()
    new_yesterday= UserTable.objects.filter(created_date__date=yesterday).count()

    total_campaigns   = Campaign.objects.count()
    sending_campaigns = Campaign.objects.filter(status__in=['scheduled', 'sending']).count()

    val_total   = ListFiles.objects.count()
    val_pending = ListFiles.objects.filter(job_status='Processing').count()
    val_failed  = ListFiles.objects.filter(job_status='Error').count()

    emails_validated = ListFiles.objects.filter(
        job_status='Complete'
    ).aggregate(t=Sum('total_count'))['t'] or 0

    emails_sent = CampaignStats.objects.aggregate(t=Sum('total_sent'))['t'] or 0

    # Credits used today
    credits_today = UsedCredits.objects.filter(
        vc_used_date__date=today
    ).aggregate(t=Sum('vc_used_credits'))['t'] or 0

    # Revenue
    payment_rev  = _safe_sum_amounts(Payment.objects.all())
    subs_rev     = _safe_sum_amounts(SubsPayment.objects.all())
    total_revenue = payment_rev + subs_rev

    active_subs = SubsPayment.objects.filter(plan_status='Active').count()

    return {
        'total_users':      total_users,
        'active_users':     active_users,
        'new_today':        new_today,
        'new_yesterday':    new_yesterday,
        'total_campaigns':  total_campaigns,
        'sending_campaigns':sending_campaigns,
        'val_total':        val_total,
        'val_pending':      val_pending,
        'val_failed':       val_failed,
        'emails_validated': emails_validated,
        'emails_sent':      emails_sent,
        'credits_today':    credits_today,
        'total_revenue':    round(total_revenue, 2),
        'active_subs':      active_subs,
    }


# ── Chart Data ────────────────────────────────────────────────────────────────

def get_user_registration_chart(days=30):
    cutoff = timezone.now() - timedelta(days=days)
    qs = (
        UserTable.objects
        .filter(created_date__gte=cutoff)
        .annotate(date=TruncDate('created_date'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    series = _date_series(days)
    return {'labels': series, 'data': _fill_series(qs, series)}


def get_validation_jobs_chart(days=14):
    cutoff = timezone.now() - timedelta(days=days)
    qs = (
        ListFiles.objects
        .filter(insert_date__gte=cutoff)
        .annotate(date=TruncDate('insert_date'))
        .values('date')
        .annotate(count=Count('file_id'))
        .order_by('date')
    )
    series = _date_series(days)
    return {'labels': series, 'data': _fill_series(qs, series)}


def get_campaign_sends_chart(days=30):
    cutoff = timezone.now() - timedelta(days=days)
    qs = (
        Campaign.objects
        .filter(sent_at__gte=cutoff, status='sent')
        .annotate(date=TruncDate('sent_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    series = _date_series(days)
    return {'labels': series, 'data': _fill_series(qs, series)}


def get_credits_usage_chart(days=14):
    cutoff = timezone.now() - timedelta(days=days)
    qs = (
        UsedCredits.objects
        .filter(vc_used_date__gte=cutoff)
        .annotate(date=TruncDate('vc_used_date'))
        .values('date')
        .annotate(count=Sum('vc_used_credits'))
        .order_by('date')
    )
    series = _date_series(days)
    return {'labels': series, 'data': _fill_series(qs, series)}


# ── Campaign Delivery Breakdown ───────────────────────────────────────────────

def get_delivery_breakdown():
    agg = CampaignStats.objects.aggregate(
        sent=Sum('total_sent'),
        delivered=Sum('total_delivered'),
        opened=Sum('total_opened'),
        clicked=Sum('total_clicked'),
        bounced=Sum('total_bounced'),
        complaints=Sum('total_complaints'),
        unsubscribed=Sum('total_unsubscribed'),
    )
    return {k: (v or 0) for k, v in agg.items()}


# ── Recent Activity ───────────────────────────────────────────────────────────

def get_recent_activity():
    recent_users = (
        UserTable.objects
        .only('id', 'user_email', 'user_name', 'company', 'is_active', 'created_date')
        .order_by('-created_date')[:6]
    )

    recent_payments = (
        Payment.objects
        .select_related('user')
        .only('id', 'user', 'payer_email', 'amount', 'currency', 'credits', 'payment_time')
        .order_by('-payment_time')[:6]
    )

    recent_campaigns = (
        Campaign.objects
        .select_related('user')
        .only('id', 'Campaign_ID', 'campaign_name', 'status', 'user', 'created_at', 'total_recipients')
        .order_by('-created_at')[:6]
    )

    failed_jobs = (
        ListFiles.objects
        .select_related('user')
        .filter(job_status='Error')
        .only('file_id', 'file_name', 'user', 'insert_date', 'total_count')
        .order_by('-insert_date')[:6]
    )

    recent_audit = (
        AdminActivity.objects
        .select_related('admin')
        .only('id', 'admin', 'action', 'module', 'target_repr', 'status', 'created_at')
        .order_by('-created_at')[:8]
    )

    return {
        'recent_users':    recent_users,
        'recent_payments': recent_payments,
        'recent_campaigns':recent_campaigns,
        'failed_jobs':     failed_jobs,
        'recent_audit':    recent_audit,
    }


# ── Full Dashboard Context ────────────────────────────────────────────────────

def get_dashboard_context():
    stats    = get_stat_cards()
    activity = get_recent_activity()
    delivery = get_delivery_breakdown()

    user_chart       = get_user_registration_chart(30)
    validation_chart = get_validation_jobs_chart(14)
    campaign_chart   = get_campaign_sends_chart(30)
    credits_chart    = get_credits_usage_chart(14)

    return {
        **stats,
        **activity,
        'delivery':          delivery,
        'user_chart':        user_chart,
        'validation_chart':  validation_chart,
        'campaign_chart':    campaign_chart,
        'credits_chart':     credits_chart,
    }
