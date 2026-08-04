import logging
from django.shortcuts import render
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors
from Email_validate_app.services.admin.dashboard_service import get_dashboard_context

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_dashboard(request):
    ctx = get_dashboard_context()
    ctx['page'] = 'dashboard'
    return render(request, 'admin/dashboard.html', ctx)
