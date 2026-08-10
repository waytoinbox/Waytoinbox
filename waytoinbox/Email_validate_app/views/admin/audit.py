import logging
from django.shortcuts import render
from django.core.paginator import Paginator
from django.db.models import Q

from Email_validate_app.models import AdminActivity
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_audit(request):
    qs = AdminActivity.objects.select_related('admin').order_by('-created_at')

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(target_repr__icontains=q) |
            Q(action__icontains=q) |
            Q(admin__user_email__icontains=q)
        )

    module_filter = request.GET.get('module', '')
    if module_filter:
        qs = qs.filter(module=module_filter)

    action_filter = request.GET.get('action', '')
    if action_filter:
        qs = qs.filter(action__icontains=action_filter)

    status_filter = request.GET.get('status', '')
    if status_filter:
        qs = qs.filter(status=status_filter)

    total = qs.count()
    page_num = request.GET.get('page', 1)
    page_obj = Paginator(qs, 50).get_page(page_num)

    modules = AdminActivity.objects.values_list('module', flat=True).distinct().order_by('module')

    return render(request, 'admin/audit/list.html', {
        'page': 'audit',
        'page_obj': page_obj,
        'total': total,
        'q': q,
        'module_filter': module_filter,
        'action_filter': action_filter,
        'status_filter': status_filter,
        'modules': modules,
    })
