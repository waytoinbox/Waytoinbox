"""Admin Campaign Management Service."""
import logging

from django.core.paginator import Paginator
from django.db import models
from django.utils import timezone

from Email_validate_app.models import Campaign, CampaignStats

logger = logging.getLogger('Email_validate_app.services')

_PER_PAGE = 25


def get_campaign_list(request_get):
    """Return (page_obj, params, total) for the campaign list."""
    qs = Campaign.objects.select_related('user', 'campaign_list').order_by('-created_at')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(campaign_name__icontains=q)
            | models.Q(user__user_email__icontains=q)
            | models.Q(from_email__icontains=q)
        )

    status_filter = request_get.get('status', '')
    if status_filter == 'archived':
        qs = qs.filter(deleted_at__isnull=False)
    elif status_filter:
        qs = qs.filter(status=status_filter, deleted_at__isnull=True)
    else:
        qs = qs.filter(deleted_at__isnull=True)

    total = qs.count()
    try:
        page_num = int(request_get.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, _PER_PAGE)
    page_obj = paginator.get_page(page_num)
    params = {'q': q, 'status': status_filter}
    return page_obj, params, total


def get_campaign_detail(cid):
    """Return dict with campaign + stats."""
    campaign = Campaign.objects.select_related('user', 'campaign_list', 'template').get(pk=cid)
    try:
        stats = campaign.stats
    except CampaignStats.DoesNotExist:
        stats = None
    return {'campaign': campaign, 'stats': stats}


def cancel_campaign(cid):
    """Cancel a draft/scheduled/sending campaign. Returns (campaign, old_status)."""
    campaign = Campaign.objects.get(pk=cid)
    if campaign.status not in ('draft', 'scheduled', 'sending'):
        raise ValueError(f'Cannot cancel a "{campaign.status}" campaign.')
    old_status = campaign.status
    campaign.status = 'cancelled'
    campaign.save(update_fields=['status', 'updated_at'])
    return campaign, old_status


def archive_campaign(cid):
    """Soft-archive by setting deleted_at. Returns campaign."""
    campaign = Campaign.objects.get(pk=cid)
    if campaign.deleted_at:
        raise ValueError('Campaign is already archived.')
    campaign.deleted_at = timezone.now()
    campaign.save(update_fields=['deleted_at', 'updated_at'])
    return campaign


def duplicate_campaign(cid):
    """Create a draft copy of a campaign. Returns new campaign."""
    src = Campaign.objects.get(pk=cid)
    new_campaign = Campaign(
        user=src.user,
        campaign_name=f'Copy of {src.campaign_name}',
        campaign_list=src.campaign_list,
        template=src.template,
        sender_name=src.sender_name,
        from_email=src.from_email,
        reply_email=src.reply_email,
        schedule_timezone=src.schedule_timezone,
        status='draft',
        total_recipients=0,
    )
    new_campaign.save()
    return new_campaign
