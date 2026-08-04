import logging

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render

from Email_validate_app.models import EmailValidate
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_ev(request):
    q = request.GET.get('q', '').strip()
    mx_filter = request.GET.get('mx', '')

    qs = EmailValidate.objects.select_related('user').filter(
        is_hidden=False
    ).order_by('-insert_date')

    if q:
        qs = qs.filter(
            Q(email__icontains=q) | Q(user__user_email__icontains=q)
        )
    if mx_filter == '1':
        qs = qs.filter(mx_found='True')
    elif mx_filter == '0':
        qs = qs.filter(mx_found='False')

    total = qs.count()
    mx_found_count = EmailValidate.objects.filter(is_hidden=False, mx_found='True').count()

    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(page_num)

    return render(request, 'admin/email_verification/index.html', {
        'page': 'email_verification',
        'page_obj': page_obj,
        'total': total,
        'mx_found_count': mx_found_count,
        'q': q,
        'mx_filter': mx_filter,
    })
