"""
views/so_analytics.py
----------------------
V2.4.9 — read-only HTTP layer over services/so_analytics.py + so_optimization.py.
No writes to any send-path model happen anywhere in this file.
"""
import logging
from datetime import datetime

from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def _auth_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)


def _parse_date(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _resolve_filters(request, tz):
    from Email_validate_app.services.so_analytics import resolve_date_range

    preset = request.GET.get('preset', 'all')
    if preset not in ('today', '7d', '30d', 'all', 'custom'):
        preset = 'all'
    date_from = _parse_date(request.GET.get('date_from', ''))
    date_to = _parse_date(request.GET.get('date_to', ''))
    start, end = resolve_date_range(preset, tz, date_from=date_from, date_to=date_to)
    return preset, date_from, date_to, start, end


# ── Campaign Detail → Analytics section (AJAX data endpoint) ───────────────

def so_campaign_analytics_data(request, cid):
    r = _auth_json(request)
    if r:
        return r
    from Email_validate_app.models import SOCampaign
    from Email_validate_app.services import so_analytics as A
    from Email_validate_app.services.so_optimization import compute_campaign_recommendations

    user_id = get_user_id(request)
    try:
        campaign = SOCampaign.objects.get(id=cid, user_id=user_id, deleted_at__isnull=True)
    except SOCampaign.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Not found.'}, status=404)

    tz = A.campaign_timezone(campaign)
    preset, date_from, date_to, start, end = _resolve_filters(request, tz)

    overview = A.compute_overview(campaign, start, end)
    steps = A.compute_step_analytics(campaign, start, end)
    variants = A.compute_variant_analytics(campaign, start, end)
    day_hour = A.compute_day_hour_analytics(campaign, start, end)

    payload = {
        'status': 'ok',
        'preset': preset,
        'date_from': date_from.isoformat() if date_from else '',
        'date_to': date_to.isoformat() if date_to else '',
        'timezone': str(tz),
        'overview': overview,
        'funnel': A.compute_funnel(campaign, start, end),
        'trend': A.compute_trend(campaign, start, end),
        'steps': steps,
        'variants': variants,
        'senders': A.compute_sender_account_analytics(user_id, start, end, campaign=campaign),
        'day_hour': day_hour,
        'branching': A.compute_branch_analytics(campaign),
        'recommendations': compute_campaign_recommendations(
            campaign, start, end, step_data=steps, variant_data=variants, day_hour=day_hour, overview=overview,
        ),
    }
    return JsonResponse(payload)


# ── Sales Outreach → Analytics (cross-campaign overview page) ──────────────

def _resolve_account(request, user_id):
    """Validates the optional ?account=<id> query param (V4.4 — Account
    Analytics) against this user's own, non-deleted accounts. Returns the
    SOEmailAccount or None. An invalid, foreign, or deleted id is silently
    treated the same as "no account" — falling back to the general,
    unscoped view — rather than erroring, so a bad id can never confirm or
    deny whether some other user's account exists."""
    from Email_validate_app.models import SOEmailAccount

    account_id = request.GET.get('account')
    if not account_id:
        return None
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return None
    return SOEmailAccount.objects.filter(
        id=account_id, user_id=user_id, deleted_at__isnull=True,
    ).first()


def so_analytics_overview(request):
    r = _auth(request)
    if r:
        return r
    user_id = get_user_id(request)
    account = _resolve_account(request, user_id)
    return render(request, 'i_SO_Analytics.html', {'account': account})


def so_analytics_overview_data(request):
    r = _auth_json(request)
    if r:
        return r
    from Email_validate_app.models import UserTable
    from Email_validate_app.services import so_analytics as A
    from Email_validate_app.services.so_optimization import compute_account_recommendations

    user_id = get_user_id(request)
    user = UserTable.objects.filter(id=user_id).first()
    tz = A.user_timezone(user)
    preset, date_from, date_to, start, end = _resolve_filters(request, tz)
    account = _resolve_account(request, user_id)
    account_id = account.id if account else None

    comparison = A.compute_campaign_comparison(user_id, start, end, account_id=account_id)
    senders = A.compute_sender_account_analytics(user_id, start, end, account_id=account_id)

    warmup = None
    if account is None:
        recommendations = compute_account_recommendations(
            user_id, start, end, accounts_data=senders, campaigns_data=comparison,
        )
        totals = {
            'campaigns': len(comparison),
            'sent': sum(c['sent'] for c in comparison),
            'delivered': sum(c['delivered'] for c in comparison),
        }
    else:
        # compute_account_recommendations compares across >=2 accounts
        # (needs at least 2 "eligible" rows) — meaningless once already
        # scoped to one, so this phase skips it rather than rendering an
        # always-empty/misleading insights block.
        recommendations = []
        acc_row = senders['accounts'][0] if senders['accounts'] else None
        totals = {
            'campaigns': len(comparison),
            'sent': acc_row['sent'] if acc_row else 0,
            'delivered': acc_row['delivered'] if acc_row else 0,
        }
        # Warmup snapshot — not date-filtered (mirrors the /Warmup/
        # dashboard's own always-today+all-time design, no preset there
        # either), so it's included as-is on every preset/date change
        # alongside the campaign data rather than needing a second request.
        from Email_validate_app.services.warmup import compute_account_warmup_analytics
        warmup = compute_account_warmup_analytics(account)

    payload = {
        'status': 'ok',
        'preset': preset,
        'date_from': date_from.isoformat() if date_from else '',
        'date_to': date_to.isoformat() if date_to else '',
        'timezone': str(tz),
        'account': {'id': account.id, 'email': account.email} if account else None,
        'totals': totals,
        'campaigns': comparison,
        'senders': senders,
        'recommendations': recommendations,
        'warmup': warmup,
    }
    return JsonResponse(payload)
