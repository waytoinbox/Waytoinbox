from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.timezone import now

from Email_validate_app.utils import get_user_id


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def _counts(qs):
    return {
        'sent':      qs.filter(sent_at__isnull=False).count(),
        'inbox':     qs.filter(landing_location='inbox').count(),
        'spam':      qs.filter(landing_location='spam').count(),
        'rescued':   qs.filter(landing_location='spam', rescued_to_inbox=True).count(),
        'other':     qs.filter(landing_location='other').count(),
        'not_found': qs.filter(landing_location='not_found').count(),
        'read':      qs.filter(marked_read=True).count(),
    }


def _pct(num, den):
    if not den:
        return None
    v = round(num / den * 100, 1)
    return int(v) if v == int(v) else v


def warmup_dashboard(request):
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOEmailAccountWarmup, WarmupMessage

    user_id = get_user_id(request)

    warmups = SOEmailAccountWarmup.objects.filter(
        account__user_id=user_id, account__deleted_at__isnull=True,
    ).select_related('account').order_by('-started_at')

    # Aggregate status banner. "Running" if anything at all is active — one
    # paused/stopped sender never masks another still-active one, matching
    # the "one sender's state never affects another" design throughout.
    if warmups.filter(status='active').exists():
        overall_status = 'running'
    elif warmups.filter(status='paused').exists():
        overall_status = 'paused'
    elif warmups.exists():
        overall_status = 'stopped'
    else:
        overall_status = 'not_configured'

    all_messages   = WarmupMessage.objects.filter(sender_account__user_id=user_id)
    today_messages = all_messages.filter(created_at__date=now().date())

    today_stats = _counts(today_messages)
    total_stats = _counts(all_messages)

    # Placement rate is computed over messages that were actually checked
    # (found somewhere), not over every message ever sent (not-yet-checked
    # 'sent'/'checking' rows would otherwise dilute the rate meaninglessly).
    checked_total = total_stats['inbox'] + total_stats['spam'] + total_stats['other']
    placement_pct = {
        'inbox': _pct(total_stats['inbox'], checked_total),
        'spam':  _pct(total_stats['spam'], checked_total),
        'other': _pct(total_stats['other'], checked_total),
    }

    sender_rows = []
    for warmup in warmups:
        acc_msgs = all_messages.filter(sender_account=warmup.account)
        sender_rows.append({
            'account':   warmup.account,
            'warmup':    warmup,
            'sent':      acc_msgs.filter(sent_at__isnull=False).count(),
            'inbox':     acc_msgs.filter(landing_location='inbox').count(),
            'spam':      acc_msgs.filter(landing_location='spam').count(),
            'other':     acc_msgs.filter(landing_location='other').count(),
            'failed':    acc_msgs.filter(status='send_failed').count(),
        })

    # Receivers are a fixed, admin-managed shared pool — end users never see
    # or manage them (see views/admin/warmup.py), so nothing about the pool
    # itself is queried or passed to this page.
    return render(request, 'i_Warmup_Dashboard.html', {
        'overall_status':      overall_status,
        'today_stats':         today_stats,
        'total_stats':         total_stats,
        'placement_pct':       placement_pct,
        'sender_rows':         sender_rows,
    })
