import logging

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render

from Email_validate_app.models import SenderDomain, SenderEmailToken
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_dmarc(request):
    q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')

    domain_qs = SenderDomain.objects.select_related('user').filter(
        is_hidden=False, deleted_at__isnull=True
    ).order_by('-added_at')

    email_qs = SenderEmailToken.objects.select_related('user').filter(
        is_hidden=False, deleted_at__isnull=True
    ).order_by('-created_at')

    if q:
        domain_qs = domain_qs.filter(
            Q(domain__icontains=q) | Q(user__user_email__icontains=q)
        )
        email_qs = email_qs.filter(
            Q(email__icontains=q) | Q(user__user_email__icontains=q)
        )

    if status_filter:
        domain_qs = domain_qs.filter(status=status_filter)

    # Summary counts
    domain_counts = dict(
        SenderDomain.objects.filter(is_hidden=False, deleted_at__isnull=True)
        .values_list('status').annotate(c=Count('id'))
    )

    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    domain_page = Paginator(domain_qs, 25).get_page(page_num)
    email_page = Paginator(email_qs, 25).get_page(page_num)

    return render(request, 'admin/dmarc/index.html', {
        'page': 'dmarc',
        'domain_page': domain_page,
        'email_page': email_page,
        'domain_total': domain_qs.count(),
        'email_total': email_qs.count(),
        'domain_counts': domain_counts,
        'q': q,
        'status_filter': status_filter,
    })
