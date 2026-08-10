import logging

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render

from Email_validate_app.models import CampaignEvent
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')

_SUPPRESSION_TYPES = ('unsubscribe', 'bounce', 'complaint')


@admin_required
@handle_admin_errors
def admin_suppression(request):
    event_type = request.GET.get('type', '')
    q = request.GET.get('q', '').strip()

    qs = CampaignEvent.objects.select_related('user', 'campaign').filter(
        event_type__in=_SUPPRESSION_TYPES
    ).order_by('-event_time')

    if event_type in _SUPPRESSION_TYPES:
        qs = qs.filter(event_type=event_type)

    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(campaign__campaign_name__icontains=q))

    counts = CampaignEvent.objects.filter(
        event_type__in=_SUPPRESSION_TYPES
    ).values('event_type').annotate(c=Count('id'))
    type_counts = {r['event_type']: r['c'] for r in counts}

    total = qs.count()
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(page_num)

    return render(request, 'admin/suppression/index.html', {
        'page': 'suppression',
        'page_obj': page_obj,
        'total': total,
        'q': q,
        'event_type': event_type,
        'type_counts': type_counts,
    })
