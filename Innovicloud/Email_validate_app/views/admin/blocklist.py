import logging

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from Email_validate_app.models import BlocklistMonitor, DomainBlocklist
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_blocklist(request):
    q = request.GET.get('q', '').strip()
    tab = request.GET.get('tab', 'ip')

    ip_qs = BlocklistMonitor.objects.select_related('user').filter(
        is_hidden=False, deleted_date__isnull=True
    ).order_by('-created_date')

    domain_qs = DomainBlocklist.objects.select_related('user').filter(
        is_hidden=False, deleted_date__isnull=True
    ).order_by('-created_date')

    if q:
        ip_qs = ip_qs.filter(Q(ips__icontains=q) | Q(user__user_email__icontains=q))
        domain_qs = domain_qs.filter(
            Q(domain__icontains=q) | Q(user__user_email__icontains=q)
        )

    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    ip_page = Paginator(ip_qs, 25).get_page(page_num)
    domain_page = Paginator(domain_qs, 25).get_page(page_num)

    return render(request, 'admin/blocklist/index.html', {
        'page': 'blocklist',
        'ip_page': ip_page,
        'domain_page': domain_page,
        'ip_total': ip_qs.count(),
        'domain_total': domain_qs.count(),
        'q': q,
        'tab': tab,
    })
