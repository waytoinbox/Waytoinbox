import logging
from django.shortcuts import render
from django.views.decorators.http import require_POST

from Email_validate_app.models import UserNotification, UserTable
from Email_validate_app.views.admin._base import (
    admin_required, handle_admin_errors, audit, json_ok, json_error,
)

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_notifications(request):
    if request.method == 'POST':
        return _send_notifications(request)

    user_count = UserTable.objects.filter(is_active=True).count()
    return render(request, 'admin/notifications/compose.html', {
        'page': 'notifications',
        'user_count': user_count,
    })


def _send_notifications(request):
    target = request.POST.get('target', 'all')
    notif_type = request.POST.get('notif_type', 'info').strip()
    message = request.POST.get('message', '').strip()

    if not message:
        return json_error('Message is required.')
    if len(message) > 255:
        return json_error('Message must be 255 characters or fewer.')

    if target == 'all':
        users = UserTable.objects.filter(is_active=True).only('pk')
    elif target == 'admins':
        users = UserTable.objects.filter(is_active=True, is_admin=True).only('pk')
    elif target == 'user':
        uid = request.POST.get('user_id', '').strip()
        if not uid:
            return json_error('User ID required for single-user target.')
        users = UserTable.objects.filter(pk=uid).only('pk')
    else:
        return json_error('Invalid target.')

    notifications = [
        UserNotification(user=u, type=notif_type, message=message)
        for u in users
    ]
    UserNotification.objects.bulk_create(notifications, batch_size=500)

    count = len(notifications)
    audit(request, 'notifications.send', 'notifications',
          notes=f'target={target} type={notif_type} count={count}')
    return json_ok(message=f'{count} notification{"s" if count != 1 else ""} sent.')
