import logging

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from Email_validate_app.models import EmailHeader
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_headers(request):
    q = request.GET.get('q', '').strip()

    qs = EmailHeader.objects.select_related('user').order_by('-created_at')

    if q:
        qs = qs.filter(
            Q(from_email__icontains=q)
            | Q(to_email__icontains=q)
            | Q(subject__icontains=q)
            | Q(user__user_email__icontains=q)
        )

    total = qs.count()
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    page_obj = Paginator(qs, 25).get_page(page_num)

    return render(request, 'admin/headers/index.html', {
        'page': 'headers',
        'page_obj': page_obj,
        'total': total,
        'q': q,
    })
