import logging

from django.shortcuts import render
from django.views.decorators.http import require_POST

from Email_validate_app.models import SubsPayment
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import payment_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_subscriptions(request):
    page_obj, params, total = payment_service.get_subscription_list(request.GET)
    return render(request, 'admin/subscriptions/list.html', {
        'page': 'subscriptions',
        'page_obj': page_obj,
        'params': params,
        'total': total,
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_subs_extend(request, sid):
    days = request.POST.get('days', '').strip()
    try:
        sub = payment_service.extend_subscription(sid, days)
    except SubsPayment.DoesNotExist:
        return json_error('Subscription not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='subscription.extend', module='subscriptions',
        target_type='subscription', target_id=sid,
        target_repr=f'{sub.user.user_email} / {sub.subs_plan}',
        new_value={
            'valid_time': sub.valid_time.isoformat() if sub.valid_time else None,
            'extended_days': int(days),
        },
    )
    return json_ok(
        data={'new_valid_time': sub.valid_time.strftime('%b %d, %Y') if sub.valid_time else '—'},
        message=f'Subscription extended by {days} days.',
    )
