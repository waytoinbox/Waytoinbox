import logging

from django.db.models import Avg, Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone
from datetime import timedelta

from Email_validate_app.models import (
    Campaign, CampaignEvent, CampaignStats, ListFiles, UserTable,
)
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors
from Email_validate_app.services.admin.dashboard_service import (
    get_campaign_sends_chart, get_user_registration_chart,
    get_validation_jobs_chart, get_credits_usage_chart,
)

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_analytics(request):
    # Aggregate campaign delivery stats
    delivery = CampaignStats.objects.aggregate(
        sent=Sum('total_sent'),
        delivered=Sum('total_delivered'),
        opened=Sum('total_opened'),
        clicked=Sum('total_clicked'),
        bounced=Sum('total_bounced'),
        complaints=Sum('total_complaints'),
        unsubscribed=Sum('total_unsubscribed'),
    )
    delivery = {k: (v or 0) for k, v in delivery.items()}

    sent = delivery['sent'] or 1
    open_rate    = round(delivery['opened']      / sent * 100, 1)
    click_rate   = round(delivery['clicked']     / sent * 100, 1)
    bounce_rate  = round(delivery['bounced']     / sent * 100, 1)
    complaint_rate = round(delivery['complaints']/ sent * 100, 2)
    unsub_rate   = round(delivery['unsubscribed']/ sent * 100, 1)

    # Campaign status distribution
    status_counts = dict(
        Campaign.objects.values_list('status').annotate(c=Count('id'))
    )

    # Validation job status distribution
    val_status = dict(
        ListFiles.objects.values_list('job_status').annotate(c=Count('file_id'))
    )

    # Top 10 users by validation jobs
    top_validators = (
        ListFiles.objects.values('user__user_email')
        .annotate(jobs=Count('file_id'), emails=Sum('total_count'))
        .order_by('-jobs')[:10]
    )

    # Top 10 users by campaigns sent
    top_campaigners = (
        Campaign.objects.filter(status='sent')
        .values('user__user_email')
        .annotate(campaigns=Count('id'), recipients=Sum('total_recipients'))
        .order_by('-campaigns')[:10]
    )

    # Recent 30 days activity for charts
    user_chart       = get_user_registration_chart(30)
    validation_chart = get_validation_jobs_chart(30)
    campaign_chart   = get_campaign_sends_chart(30)
    credits_chart    = get_credits_usage_chart(30)

    return render(request, 'admin/analytics/index.html', {
        'page': 'analytics',
        'delivery': delivery,
        'open_rate': open_rate,
        'click_rate': click_rate,
        'bounce_rate': bounce_rate,
        'complaint_rate': complaint_rate,
        'unsub_rate': unsub_rate,
        'status_counts': status_counts,
        'val_status': val_status,
        'top_validators': top_validators,
        'top_campaigners': top_campaigners,
        'user_chart': user_chart,
        'validation_chart': validation_chart,
        'campaign_chart': campaign_chart,
        'credits_chart': credits_chart,
    })
