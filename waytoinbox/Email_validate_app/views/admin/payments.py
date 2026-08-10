import logging

from django.shortcuts import render

from Email_validate_app.views.admin._base import admin_required, handle_admin_errors
from Email_validate_app.services.admin import payment_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_payments(request):
    page_obj, params, total, total_revenue = payment_service.get_payment_list(request.GET)
    return render(request, 'admin/payments/list.html', {
        'page': 'payments',
        'page_obj': page_obj,
        'params': params,
        'total': total,
        'total_revenue': total_revenue,
    })
