import logging

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from Email_validate_app.models import Reputation, ReputationResults
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_reputation(request):
    q = request.GET.get('q', '').strip()

    qs = Reputation.objects.select_related('user').filter(
        is_hidden=False, deleted_at__isnull=True
    ).order_by('-created_at')

    if q:
        qs = qs.filter(Q(domain__icontains=q) | Q(user__user_email__icontains=q))

    total = qs.count()
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    page_obj = Paginator(qs, 25).get_page(page_num)

    # Latest results per domain (for status display)
    recent_results = (
        ReputationResults.objects
        .order_by('-date')
        .values('domain', 'domain_reputation', 'spam_rate', 'date')[:50]
    )
    results_map = {r['domain']: r for r in recent_results}

    return render(request, 'admin/reputation/index.html', {
        'page': 'reputation',
        'page_obj': page_obj,
        'total': total,
        'q': q,
        'results_map': results_map,
    })
