"""
Warmup sender enrollment/control action endpoint. There is no separate
"Warmup Senders" page — enrollment status and Start/Pause/Resume/Stop
controls live directly on the existing SO Email Accounts page
(views/so_email_accounts.py, templates/i_SO_Email_Accounts.html), which
calls this same JSON action endpoint.
"""

import json

from django.http import JsonResponse

from Email_validate_app.utils import get_user_id


def _auth_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)


def warmup_sender_action(request):
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOEmailAccount
    from Email_validate_app.services import warmup as warmup_service

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)

    action  = data.get('action')
    user_id = get_user_id(request)
    raw_ids = data.get('ids') or ([data.get('id')] if data.get('id') else [])
    raw_ids = [i for i in raw_ids if i]

    # Scope strictly to this user's own, non-deleted accounts — never trust
    # ids from the request body directly.
    valid_ids = list(SOEmailAccount.objects.filter(
        id__in=raw_ids, user_id=user_id, deleted_at__isnull=True,
    ).values_list('id', flat=True))
    if not valid_ids:
        return JsonResponse({'status': 'error', 'message': 'No valid accounts selected.'})

    if action == 'start':
        def _int_or_none(v):
            try:
                return int(v) if v not in (None, '') else None
            except (TypeError, ValueError):
                return None

        warmup_service.start_warmup(
            valid_ids,
            daily_target=_int_or_none(data.get('daily_target')),
            ramp_up_days=_int_or_none(data.get('ramp_up_days')),
            ramp_up_increment=_int_or_none(data.get('ramp_up_increment')),
        )
        return JsonResponse({'status': 'ok'})

    if action == 'pause':
        warmup_service.pause_warmup(valid_ids)
        return JsonResponse({'status': 'ok'})

    if action == 'resume':
        warmup_service.resume_warmup(valid_ids)
        return JsonResponse({'status': 'ok'})

    if action == 'stop':
        warmup_service.stop_warmup(valid_ids)
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})
