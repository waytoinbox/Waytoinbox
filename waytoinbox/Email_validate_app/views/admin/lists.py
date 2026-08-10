import logging

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from Email_validate_app.models import CampaignList, CampaignEmail
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_lists(request):
    qs = CampaignList.objects.select_related('user').filter(
        deleted_at__isnull=True
    ).order_by('-created_at')

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(list_name__icontains=q) | Q(user__user_email__icontains=q))

    total = qs.count()
    paginator = Paginator(qs, 25)
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    return render(request, 'admin/lists/list.html', {
        'page': 'lists',
        'page_obj': paginator.get_page(page_num),
        'total': total,
        'q': q,
    })


@admin_required
@handle_admin_errors
def admin_list_detail(request, lid):
    campaign_list = get_object_or_404(CampaignList, pk=lid)
    emails_qs = CampaignEmail.objects.filter(
        list=campaign_list, deleted_at__isnull=True
    ).order_by('-created_at')

    paginator = Paginator(emails_qs, 50)
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    return render(request, 'admin/lists/detail.html', {
        'page': 'lists',
        'campaign_list': campaign_list,
        'email_page': paginator.get_page(page_num),
    })
