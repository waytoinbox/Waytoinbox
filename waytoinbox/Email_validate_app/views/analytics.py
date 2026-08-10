import json
import logging
from datetime import datetime, timedelta

from collections import defaultdict

from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone

def analytics_overview(request):
    if _login_required(request):
        return redirect('login')
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')
    date_from, date_to, preset = _parse_date_range(request)
    return render(request, 'i_analytics_overview.html', {
        'preset':    preset,
        'date_from': str(date_from),
        'date_to':   str(date_to),
    })


from Email_validate_app.models import (
    ListFiles, EmailValidate, CreditAuditLog,
    Campaign, CampaignStats, CampaignEvent,
    BlocklistMonitor, BlacklistStatus, BlacklistListed,
    DomainBlocklist, DomainBlacklistStatus, DomainBlacklistListed,
    Reputation, ReputationResults, DMARCAnalysis, EmailHeader,
)
from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)


def _login_required(request):
    return not request.session.get('logged_in')


def _parse_date_range(request):
    preset = request.GET.get('preset', '7d')
    today = timezone.now().date()

    if preset == 'today':
        return today, today, 'today'
    if preset == '7d':
        return today - timedelta(days=6), today, '7d'
    if preset == 'month':
        return today.replace(day=1), today, 'month'
    if preset == 'custom':
        try:
            df = datetime.strptime(request.GET.get('date_from', ''), '%Y-%m-%d').date()
            dt = datetime.strptime(request.GET.get('date_to',   ''), '%Y-%m-%d').date()
            return df, dt, 'custom'
        except ValueError:
            pass
    return today - timedelta(days=6), today, '7d'


def email_validation_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    is_ajax = request.GET.get('format') == 'json'

    # ── Bulk validation (ListFiles) ──────────────────────────────────────────
    bulk_qs = ListFiles.objects.filter(
        user_id=user_id,
        insert_date__date__range=[date_from, date_to],
    )
    agg = bulk_qs.aggregate(
        total_bulk=Sum('total_count'),
        valid=Sum('valid_count'),
        invalid=Sum('invalid_count'),
        risky=Sum('unknown_count'),
        others=Sum('others_count'),
    )

    bulk_jobs     = bulk_qs.count()
    bulk_total    = agg['total_bulk'] or 0
    valid_count   = agg['valid']      or 0
    invalid_count = agg['invalid']    or 0
    risky_count   = agg['risky']      or 0
    others_count  = agg['others']     or 0

    # ── Single validation (EmailValidate) ────────────────────────────────────
    single_qs = EmailValidate.objects.filter(
        user_id=user_id,
        insert_date__date__range=[date_from, date_to],
        is_hidden=False,
    )
    single_count = single_qs.count()

    # mx_found stores the result: Valid / Invalid / Risky / Others
    single_breakdown = {
        row['mx_found']: row['cnt']
        for row in single_qs.values('mx_found').annotate(cnt=Count('id'))
    }

    # ── Combined totals ──────────────────────────────────────────────────────
    total_emails  = bulk_total + single_count
    valid_count   += single_breakdown.get('Valid',   0)
    invalid_count += single_breakdown.get('Invalid', 0)
    risky_count   += single_breakdown.get('Risky',   0)
    others_count  += single_breakdown.get('Others',  0)

    def pct(n):
        return round(n / total_emails * 100, 1) if total_emails else 0.0

    # ── Credits used ─────────────────────────────────────────────────────────
    credits_used = (
        CreditAuditLog.objects.filter(
            user_id=user_id,
            entry_type='debit',
            ref_type='validation',
            created_at__date__range=[date_from, date_to],
        ).aggregate(total=Sum('amount'))['total'] or 0
    )

    # ── Chart 2: Line — daily trend (bulk + single merged) ───────────────────
    bulk_daily = (
        bulk_qs
        .annotate(day=TruncDate('insert_date'))
        .values('day')
        .annotate(cnt=Sum('total_count'))
        .order_by('day')
    )
    single_daily = (
        single_qs
        .annotate(day=TruncDate('insert_date'))
        .values('day')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )

    trend_map = {}
    for r in bulk_daily:
        k = str(r['day'])
        trend_map[k] = trend_map.get(k, 0) + (r['cnt'] or 0)
    for r in single_daily:
        k = str(r['day'])
        trend_map[k] = trend_map.get(k, 0) + r['cnt']

    trend_labels = sorted(trend_map.keys())
    trend_data   = [trend_map[d] for d in trend_labels]

    # ── Chart 4: Stacked bar — daily result breakdown (bulk + single) ─────────
    stacked_map = defaultdict(lambda: {'v': 0, 'inv': 0, 'r': 0, 'o': 0})

    for row in (
        bulk_qs
        .annotate(day=TruncDate('insert_date'))
        .values('day')
        .annotate(v=Sum('valid_count'), inv=Sum('invalid_count'),
                  r=Sum('unknown_count'), o=Sum('others_count'))
        .order_by('day')
    ):
        k = str(row['day'])
        stacked_map[k]['v']   += row['v']   or 0
        stacked_map[k]['inv'] += row['inv'] or 0
        stacked_map[k]['r']   += row['r']   or 0
        stacked_map[k]['o']   += row['o']   or 0

    _rkey = {'Valid': 'v', 'Invalid': 'inv', 'Risky': 'r', 'Others': 'o'}
    for row in (
        single_qs
        .annotate(day=TruncDate('insert_date'))
        .values('day', 'mx_found')
        .annotate(cnt=Count('id'))
        .order_by('day')
    ):
        stacked_map[str(row['day'])][_rkey.get(row['mx_found'], 'o')] += row['cnt']

    stacked_labels  = sorted(stacked_map.keys())
    stacked_valid   = [stacked_map[d]['v']   for d in stacked_labels]
    stacked_invalid = [stacked_map[d]['inv'] for d in stacked_labels]
    stacked_risky   = [stacked_map[d]['r']   for d in stacked_labels]
    stacked_others  = [stacked_map[d]['o']   for d in stacked_labels]

    # ── Assemble chart data ──────────────────────────────────────────────────
    chart_data = {
        'donut': {
            'data': [valid_count, invalid_count, risky_count, others_count],
        },
        'trend': {
            'labels': trend_labels,
            'data':   trend_data,
        },
        'bar': {
            'data': [single_count, bulk_total],
        },
        'stacked': {
            'labels':  stacked_labels,
            'valid':   stacked_valid,
            'invalid': stacked_invalid,
            'risky':   stacked_risky,
            'others':  stacked_others,
        },
    }

    summary = {
        'total':       total_emails,
        'valid':       valid_count,
        'invalid':     invalid_count,
        'risky':       risky_count,
        'others':      others_count,
        'valid_pct':   pct(valid_count),
        'invalid_pct': pct(invalid_count),
        'risky_pct':   pct(risky_count),
        'others_pct':  pct(others_count),
        'credits_used': credits_used,
        'bulk_jobs':    bulk_jobs,
        'single_jobs':  single_count,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_email_analytics.html', {
        **summary,
        'preset':          preset,
        'date_from':       str(date_from),
        'date_to':         str(date_to),
        'chart_data_json': json.dumps(chart_data),
    })


def campaign_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    status_filter  = request.GET.get('status', 'all').strip()
    campaign_id    = request.GET.get('campaign_id', '').strip()
    is_ajax        = request.GET.get('format') == 'json'

    def _rate(n, d): return round(n / d * 100, 1) if d else 0.0

    # ── Base campaign queryset (for date range) ──────────────────────────────
    base_qs = Campaign.objects.filter(
        user_id=user_id,
        deleted_at__isnull=True,
        created_at__date__range=[date_from, date_to],
    )

    # Apply additional filters
    campaign_qs = base_qs
    if status_filter not in ('', 'all'):
        campaign_qs = campaign_qs.filter(status=status_filter)
    if campaign_id:
        try:
            campaign_qs = campaign_qs.filter(Campaign_ID=int(campaign_id))
        except (ValueError, TypeError):
            pass

    # ── Summary stats ────────────────────────────────────────────────────────
    total_campaigns = campaign_qs.count()

    stats_agg = CampaignStats.objects.filter(campaign__in=campaign_qs).aggregate(
        sent=Sum('total_sent'),
        delivered=Sum('total_delivered'),
        opened=Sum('total_opened'),
        clicked=Sum('total_clicked'),
        bounced=Sum('total_bounced'),
        complaints=Sum('total_complaints'),
        unsubscribed=Sum('total_unsubscribed'),
    )
    sent         = stats_agg['sent']         or 0
    delivered    = stats_agg['delivered']    or 0
    opened       = stats_agg['opened']       or 0
    clicked      = stats_agg['clicked']      or 0
    bounced      = stats_agg['bounced']      or 0
    complaints   = stats_agg['complaints']   or 0
    unsubscribed = stats_agg['unsubscribed'] or 0

    open_rate        = _rate(opened,       delivered)
    click_rate       = _rate(clicked,      delivered)
    bounce_rate      = _rate(bounced,      sent)
    complaint_rate   = _rate(complaints,   delivered)
    unsubscribe_rate = _rate(unsubscribed, delivered)

    delivery_rate = _rate(delivered, sent)

    # ── Credits used ─────────────────────────────────────────────────────────
    credits_used = (
        CreditAuditLog.objects.filter(
            user_id=user_id,
            entry_type='debit',
            ref_type='campaign',
            created_at__date__range=[date_from, date_to],
        ).aggregate(total=Sum('amount'))['total'] or 0
    )

    # ── Event queryset (for trend charts) ────────────────────────────────────
    event_base = CampaignEvent.objects.filter(
        user_id=user_id,
        event_time__date__range=[date_from, date_to],
    )
    if campaign_id:
        try:
            event_base = event_base.filter(campaign_id=int(campaign_id))
        except (ValueError, TypeError):
            pass
    elif status_filter not in ('', 'all'):
        event_base = event_base.filter(campaign__in=campaign_qs)

    # ── Chart 2: Line — daily send trend ─────────────────────────────────────
    trend_qs = (
        event_base.filter(event_type='send')
        .annotate(day=TruncDate('event_time'))
        .values('day')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )
    trend_labels = [str(r['day']) for r in trend_qs]
    trend_data   = [r['cnt'] for r in trend_qs]

    # ── Chart 3: Bar — campaign status counts (always full date range) ────────
    status_qs = base_qs.values('status').annotate(cnt=Count('Campaign_ID'))
    status_counts = {s: 0 for s in ('draft', 'scheduled', 'sending', 'sent', 'failed', 'cancelled')}
    for row in status_qs:
        if row['status'] in status_counts:
            status_counts[row['status']] = row['cnt']

    # ── Chart 4: Stacked bar — daily engagement ───────────────────────────────
    engage_qs = (
        event_base.filter(event_type__in=['delivery', 'open', 'click', 'bounce'])
        .annotate(day=TruncDate('event_time'))
        .values('day', 'event_type')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )
    engage_map = defaultdict(lambda: {'delivery': 0, 'open': 0, 'click': 0, 'bounce': 0})
    for row in engage_qs:
        engage_map[str(row['day'])][row['event_type']] = row['cnt']
    engage_labels = sorted(engage_map.keys())
    engagement = {
        'labels':    engage_labels,
        'delivered': [engage_map[d]['delivery'] for d in engage_labels],
        'opened':    [engage_map[d]['open']     for d in engage_labels],
        'clicked':   [engage_map[d]['click']    for d in engage_labels],
        'bounced':   [engage_map[d]['bounce']   for d in engage_labels],
    }

    # ── Performance table ─────────────────────────────────────────────────────
    top_qs = (
        Campaign.objects
        .select_related('stats')
        .filter(
            user_id=user_id,
            deleted_at__isnull=True,
            created_at__date__range=[date_from, date_to],
            stats__isnull=False,
        )
        .order_by('-stats__total_opened')[:10]
    )
    if status_filter not in ('', 'all'):
        top_qs = top_qs.filter(status=status_filter)
    if campaign_id:
        try:
            top_qs = top_qs.filter(Campaign_ID=int(campaign_id))
        except (ValueError, TypeError):
            pass

    table_rows = []
    for c in top_qs:
        try:
            s = c.stats
        except Exception:
            s = None
        c_sent      = getattr(s, 'total_sent',      0) or 0
        c_delivered = getattr(s, 'total_delivered',  0) or 0
        c_opened    = getattr(s, 'total_opened',     0) or 0
        c_clicked   = getattr(s, 'total_clicked',    0) or 0
        c_bounced   = getattr(s, 'total_bounced',    0) or 0
        table_rows.append({
            'name':        c.campaign_name,
            'status':      c.status,
            'recipients':  c.total_recipients,
            'sent':        c_sent,
            'delivered':   c_delivered,
            'open_rate':   _rate(c_opened,  c_delivered),
            'click_rate':  _rate(c_clicked, c_delivered),
            'bounce_rate': _rate(c_bounced, c_sent),
            'created':     c.created_at.strftime('%b %d, %Y') if c.created_at else '—',
        })

    # ── All campaigns for dropdown ────────────────────────────────────────────
    all_campaigns = list(
        Campaign.objects.filter(user_id=user_id, deleted_at__isnull=True)
        .values('Campaign_ID', 'campaign_name')
        .order_by('-created_at')[:200]
    )

    # ── Assemble chart data ───────────────────────────────────────────────────
    chart_data = {
        'performance': {
            'delivered':  delivered,
            'opened':     opened,
            'clicked':    clicked,
            'bounced':    bounced,
            'complaints': complaints,
        },
        'sending_trend': {
            'labels': trend_labels,
            'data':   trend_data,
        },
        'status': status_counts,
        'engagement': engagement,
    }

    summary = {
        'campaigns':        total_campaigns,
        'sent':             sent,
        'delivered':        delivered,
        'opened':           opened,
        'clicked':          clicked,
        'bounced':          bounced,
        'complaints':       complaints,
        'unsubscribed':     unsubscribed,
        'credits_used':     credits_used,
        'delivery_rate':    delivery_rate,
        'open_rate':        open_rate,
        'click_rate':       click_rate,
        'bounce_rate':      bounce_rate,
        'complaint_rate':   complaint_rate,
        'unsubscribe_rate': unsubscribe_rate,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_campaign_analytics.html', {
        **summary,
        'preset':            preset,
        'date_from':         str(date_from),
        'date_to':           str(date_to),
        'status_filter':     status_filter,
        'campaign_id':       campaign_id,
        'all_campaigns':     all_campaigns,
        'table_rows':        table_rows,
        'chart_data_json':   json.dumps(chart_data),
    })


def blocklist_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    scan_type     = request.GET.get('scan_type', 'all').strip()
    result_filter = request.GET.get('result', 'all').strip()
    is_ajax       = request.GET.get('format') == 'json'

    # ── User's monitored items ────────────────────────────────────────────────
    ip_ids  = list(BlocklistMonitor.objects.filter(
        user_id=user_id, is_hidden=False
    ).values_list('ip_id', flat=True))
    dom_ids = list(DomainBlocklist.objects.filter(
        user_id=user_id, is_hidden=False
    ).values_list('domain_id', flat=True))

    # ── Base status querysets ─────────────────────────────────────────────────
    ip_base  = BlacklistStatus.objects.filter(
        ip_id__in=ip_ids, created_date__date__range=[date_from, date_to]
    )
    dom_base = DomainBlacklistStatus.objects.filter(
        domain_id__in=dom_ids, created_date__date__range=[date_from, date_to]
    )

    # Apply scan type filter
    if scan_type == 'ip':
        dom_base = dom_base.none()
    elif scan_type == 'domain':
        ip_base = ip_base.none()

    # ── Summary counts (always full — not filtered by result) ─────────────────
    ip_scans     = ip_base.count()
    domain_scans = dom_base.count()
    total_scans  = ip_scans + domain_scans

    ip_listed    = ip_base.filter(status='Listed').count()
    dom_listed   = dom_base.filter(status='Listed').count()
    listed       = ip_listed + dom_listed
    clean        = total_scans - listed
    failed       = 0  # not tracked — DNS exceptions stored as Unlisted
    health_score = round(clean / total_scans * 100, 1) if total_scans else 100.0

    # ── Apply result filter to querysets (for trend charts) ───────────────────
    ip_qs  = ip_base
    dom_qs = dom_base
    if result_filter == 'clean':
        ip_qs  = ip_qs.filter(status='Unlisted')
        dom_qs = dom_qs.filter(status='Unlisted')
    elif result_filter == 'listed':
        ip_qs  = ip_qs.filter(status='Listed')
        dom_qs = dom_qs.filter(status='Listed')
    elif result_filter == 'failed':
        ip_qs  = ip_qs.none()
        dom_qs = dom_qs.none()

    # ── Chart 1: Doughnut — distribution ─────────────────────────────────────
    distribution = {'clean': clean, 'listed': listed, 'failed': failed}

    # ── Chart 2: Line — daily scan trend ─────────────────────────────────────
    ip_daily  = ip_qs.annotate(day=TruncDate('created_date')).values('day').annotate(cnt=Count('id')).order_by('day')
    dom_daily = dom_qs.annotate(day=TruncDate('created_date')).values('day').annotate(cnt=Count('id')).order_by('day')

    trend_map = {}
    for r in ip_daily:
        k = str(r['day'])
        trend_map[k] = trend_map.get(k, 0) + r['cnt']
    for r in dom_daily:
        k = str(r['day'])
        trend_map[k] = trend_map.get(k, 0) + r['cnt']
    trend_labels = sorted(trend_map.keys())
    trend_data   = [trend_map[d] for d in trend_labels]

    # ── Chart 3: Bar — IP vs Domain total checks ──────────────────────────────
    type_chart = {'ip': ip_scans, 'domain': domain_scans}

    # ── Chart 4: Stacked bar — daily clean vs listed ──────────────────────────
    ip_det  = ip_base.annotate(day=TruncDate('created_date')).values('day', 'status').annotate(cnt=Count('id')).order_by('day')
    dom_det = dom_base.annotate(day=TruncDate('created_date')).values('day', 'status').annotate(cnt=Count('id')).order_by('day')

    det_map = defaultdict(lambda: {'clean': 0, 'listed': 0, 'failed': 0})
    for r in ip_det:
        d = str(r['day'])
        if r['status'] == 'Listed':
            det_map[d]['listed'] += r['cnt']
        else:
            det_map[d]['clean']  += r['cnt']
    for r in dom_det:
        d = str(r['day'])
        if r['status'] == 'Listed':
            det_map[d]['listed'] += r['cnt']
        else:
            det_map[d]['clean']  += r['cnt']
    det_labels = sorted(det_map.keys())
    detection = {
        'labels': det_labels,
        'clean':  [det_map[d]['clean']  for d in det_labels],
        'listed': [det_map[d]['listed'] for d in det_labels],
        'failed': [0 for _ in det_labels],
    }

    # ── Section 4: Blacklist Source Analysis (listed events only) ────────────
    ip_listed_qs  = BlacklistListed.objects.filter(
        ip_id__in=ip_ids, created_date__date__range=[date_from, date_to]
    )
    dom_listed_qs = DomainBlacklistListed.objects.filter(
        domain_id__in=dom_ids, created_date__date__range=[date_from, date_to]
    )
    if scan_type == 'ip':
        dom_listed_qs = dom_listed_qs.none()
    elif scan_type == 'domain':
        ip_listed_qs  = ip_listed_qs.none()

    ip_bl_counts  = ip_listed_qs.values('blacklists_name').annotate(cnt=Count('id')).order_by('-cnt')
    dom_bl_counts = dom_listed_qs.values('blacklists_name').annotate(cnt=Count('id')).order_by('-cnt')

    source_map = defaultdict(lambda: {'ip': 0, 'domain': 0, 'total': 0})
    for r in ip_bl_counts:
        source_map[r['blacklists_name']]['ip']    += r['cnt']
        source_map[r['blacklists_name']]['total'] += r['cnt']
    for r in dom_bl_counts:
        source_map[r['blacklists_name']]['domain'] += r['cnt']
        source_map[r['blacklists_name']]['total']  += r['cnt']

    total_detections = sum(v['total'] for v in source_map.values())
    source_table = sorted(
        [
            {
                'name':         name,
                'ip_count':     v['ip'],
                'domain_count': v['domain'],
                'total':        v['total'],
                'pct':          round(v['total'] / total_detections * 100, 1) if total_detections else 0.0,
            }
            for name, v in source_map.items()
        ],
        key=lambda x: -x['total']
    )[:20]

    # ── Section 5: Listed Item Analysis ──────────────────────────────────────
    ip_items  = list(ip_listed_qs.values('ips', 'blacklists_name', 'status', 'created_date').order_by('-created_date')[:15])
    dom_items = list(dom_listed_qs.values('domains', 'blacklists_name', 'status', 'created_date').order_by('-created_date')[:15])

    listed_items = []
    for it in ip_items:
        listed_items.append({
            'value':    it['ips'],
            'type':     'IP',
            'blacklist': it['blacklists_name'],
            'status':   it['status'],
            'date':     it['created_date'].strftime('%b %d, %Y') if it['created_date'] else '—',
        })
    for it in dom_items:
        listed_items.append({
            'value':    it['domains'],
            'type':     'Domain',
            'blacklist': it['blacklists_name'],
            'status':   it['status'],
            'date':     it['created_date'].strftime('%b %d, %Y') if it['created_date'] else '—',
        })
    listed_items.sort(key=lambda x: x['date'], reverse=True)
    listed_items = listed_items[:20]

    # ── Assemble ──────────────────────────────────────────────────────────────
    chart_data = {
        'distribution': distribution,
        'trend':        {'labels': trend_labels, 'data': trend_data},
        'type':         type_chart,
        'detection':    detection,
    }
    summary = {
        'total_scans':  total_scans,
        'clean':        clean,
        'listed':       listed,
        'failed':       failed,
        'ip_scans':     ip_scans,
        'domain_scans': domain_scans,
        'health_score': health_score,
        'clean_pct':    round(clean  / total_scans * 100, 1) if total_scans else 0.0,
        'listed_pct':   round(listed / total_scans * 100, 1) if total_scans else 0.0,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_blocklist_analytics.html', {
        **summary,
        'preset':        preset,
        'date_from':     str(date_from),
        'date_to':       str(date_to),
        'scan_type':     scan_type,
        'result_filter': result_filter,
        'source_table':  source_table,
        'listed_items':  listed_items,
        'chart_data_json': json.dumps(chart_data),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Reputation Analytics
# ─────────────────────────────────────────────────────────────────────────────

_REP_SCORE = {'HIGH': 100, 'MEDIUM': 75, 'LOW': 50, 'BAD': 25}
_REP_LABEL = {'HIGH': 'Good', 'MEDIUM': 'Medium', 'LOW': 'Poor', 'BAD': 'Critical'}
_RISK_TO_REP = {'good': 'HIGH', 'medium': 'MEDIUM', 'poor': 'LOW', 'critical': 'BAD'}


def reputation_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    analysis_type = request.GET.get('analysis_type', 'all').strip()
    risk_level    = request.GET.get('risk_level', 'all').strip()
    is_ajax       = request.GET.get('format') == 'json'

    # All user domains (reputation checks persist indefinitely)
    user_domains  = list(Reputation.objects.filter(
        user_id=user_id, is_hidden=False
    ).values_list('domain', flat=True))
    total_checks  = len(user_domains)

    # Daily reputation data in date range
    results_qs = ReputationResults.objects.filter(
        domain__in=user_domains, date__range=[date_from, date_to]
    )

    # Apply analysis-type filter
    if analysis_type == 'domain':
        results_qs = results_qs.exclude(domain_reputation__isnull=True)
    elif analysis_type == 'ip':
        results_qs = results_qs.exclude(ip_reputation=[])

    # Apply risk-level filter
    if risk_level in _RISK_TO_REP:
        results_qs = results_qs.filter(domain_reputation=_RISK_TO_REP[risk_level])

    # Load into Python for multi-axis aggregation (dataset is small)
    rows = list(results_qs.values('date', 'domain_reputation', 'spam_rate', 'ip_reputation'))

    # ── Summary distribution ──────────────────────────────────────────────────
    good     = sum(1 for r in rows if r['domain_reputation'] == 'HIGH')
    medium   = sum(1 for r in rows if r['domain_reputation'] == 'MEDIUM')
    poor     = sum(1 for r in rows if r['domain_reputation'] == 'LOW')
    critical = sum(1 for r in rows if r['domain_reputation'] == 'BAD')

    # Average score from domain_reputation mapping
    scored    = [_REP_SCORE[r['domain_reputation']] for r in rows if r['domain_reputation'] in _REP_SCORE]
    avg_score = round(sum(scored) / len(scored), 1) if scored else 0.0

    # Domain vs IP data availability counts (for Chart 3)
    domain_count = sum(1 for r in rows if r['domain_reputation'])
    ip_count     = sum(1 for r in rows if r['ip_reputation'])

    # ── Chart 2: Line — daily avg score ──────────────────────────────────────
    trend_map = defaultdict(list)
    for r in rows:
        d = str(r['date'])
        if r['domain_reputation'] in _REP_SCORE:
            trend_map[d].append(_REP_SCORE[r['domain_reputation']])
        elif r['spam_rate'] is not None:
            trend_map[d].append(max(0, round((1 - r['spam_rate']) * 100, 1)))
    trend_labels = sorted(trend_map.keys())
    trend_data   = [round(sum(trend_map[d]) / len(trend_map[d]), 1) for d in trend_labels]

    # ── Chart 4: Stacked — daily risk counts ─────────────────────────────────
    risk_day = defaultdict(lambda: {'good': 0, 'medium': 0, 'poor': 0, 'critical': 0})
    for r in rows:
        d   = str(r['date'])
        rep = r['domain_reputation']
        if rep == 'HIGH':   risk_day[d]['good']     += 1
        elif rep == 'MEDIUM': risk_day[d]['medium']  += 1
        elif rep == 'LOW':  risk_day[d]['poor']      += 1
        elif rep == 'BAD':  risk_day[d]['critical']  += 1
    risk_labels = sorted(risk_day.keys())
    risk_trend  = {
        'labels':   risk_labels,
        'good':     [risk_day[d]['good']     for d in risk_labels],
        'medium':   [risk_day[d]['medium']   for d in risk_labels],
        'poor':     [risk_day[d]['poor']     for d in risk_labels],
        'critical': [risk_day[d]['critical'] for d in risk_labels],
    }

    # ── Table ─────────────────────────────────────────────────────────────────
    rep_records = Reputation.objects.filter(
        user_id=user_id, is_hidden=False
    ).order_by('-created_at')[:20]

    latest_map = {}
    for dom in [r.domain for r in rep_records]:
        lr = ReputationResults.objects.filter(domain=dom).order_by('-date').values(
            'domain_reputation', 'spam_rate'
        ).first()
        latest_map[dom] = lr

    table_rows = []
    for rep in rep_records:
        lr    = latest_map.get(rep.domain)
        dr    = (lr or {}).get('domain_reputation')
        score = _REP_SCORE.get(dr, 0)
        risk  = _REP_LABEL.get(dr, '—')
        table_rows.append({
            'target': rep.domain,
            'type':   'Domain',
            'score':  score,
            'risk':   risk,
            'status': rep.status,
            'date':   rep.created_at.strftime('%b %d, %Y'),
        })

    chart_data = {
        'distribution': {'good': good, 'medium': medium, 'poor': poor, 'critical': critical},
        'trend':        {'labels': trend_labels, 'data': trend_data},
        'type':         {'domain': domain_count, 'ip': ip_count},
        'risk':         risk_trend,
    }
    summary = {
        'total_checks': total_checks,
        'avg_score':    avg_score,
        'good':         good,
        'medium':       medium,
        'poor':         poor,
        'critical':     critical,
        'health_score': avg_score,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_reputation_analytics.html', {
        **summary,
        'preset':         preset,
        'date_from':      str(date_from),
        'date_to':        str(date_to),
        'analysis_type':  analysis_type,
        'risk_level':     risk_level,
        'table_rows':     table_rows,
        'chart_data_json': json.dumps(chart_data),
    })


# ─────────────────────────────────────────────────────────────────────────────
# DMARC Analytics
# ─────────────────────────────────────────────────────────────────────────────

def dmarc_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    policy_filter = request.GET.get('policy', 'all').strip()
    result_filter = request.GET.get('result', 'all').strip()
    is_ajax       = request.GET.get('format') == 'json'

    qs = DMARCAnalysis.objects.filter(
        user_id=user_id, is_hidden=False,
        created_at__date__range=[date_from, date_to],
    )

    # Apply filters
    if policy_filter not in ('', 'all'):
        qs = qs.filter(dmarc_policy=policy_filter)
    if result_filter == 'pass':
        qs = qs.filter(dmarc_status='pass')
    elif result_filter == 'fail':
        qs = qs.filter(dmarc_status='fail')

    # ── Summary ───────────────────────────────────────────────────────────────
    total      = qs.count()
    spf_pass   = qs.filter(spf_status='pass').count()
    dkim_pass  = qs.filter(dkim_status='pass').count()
    dmarc_pass = qs.filter(dmarc_status='pass').count()
    dmarc_fail = total - dmarc_pass
    pass_rate  = round(dmarc_pass / total * 100, 1) if total else 0.0

    # ── Chart 2: Line — daily check count ────────────────────────────────────
    daily_qs = (
        qs.annotate(day=TruncDate('created_at'))
        .values('day').annotate(cnt=Count('id')).order_by('day')
    )
    trend_labels = [str(r['day']) for r in daily_qs]
    trend_data   = [r['cnt'] for r in daily_qs]

    # ── Chart 4: Policy distribution ─────────────────────────────────────────
    policy_raw  = qs.values('dmarc_policy').annotate(cnt=Count('id'))
    policy_dist = {'none': 0, 'quarantine': 0, 'reject': 0, 'unknown': 0}
    for r in policy_raw:
        p = (r['dmarc_policy'] or '').lower().strip()
        if p in ('none', 'quarantine', 'reject'):
            policy_dist[p] = r['cnt']
        else:
            policy_dist['unknown'] += r['cnt']

    # ── Table ─────────────────────────────────────────────────────────────────
    table_qs = qs.values(
        'domain', 'dmarc_policy', 'spf_status', 'dkim_status', 'dmarc_status', 'created_at'
    ).order_by('-created_at')[:20]
    table_rows = [
        {
            'domain':  r['domain'],
            'policy':  r['dmarc_policy'] or 'none',
            'spf':     r['spf_status'],
            'dkim':    r['dkim_status'],
            'dmarc':   r['dmarc_status'],
            'result':  r['dmarc_status'],
            'date':    r['created_at'].strftime('%b %d, %Y') if r['created_at'] else '—',
        }
        for r in table_qs
    ]

    chart_data = {
        'distribution': {'pass': dmarc_pass, 'fail': dmarc_fail},
        'trend':        {'labels': trend_labels, 'data': trend_data},
        'auth':         {'spf': spf_pass, 'dkim': dkim_pass, 'dmarc': dmarc_pass},
        'policy':       policy_dist,
    }
    summary = {
        'total':      total,
        'pass_rate':  pass_rate,
        'spf_pass':   spf_pass,
        'dkim_pass':  dkim_pass,
        'dmarc_pass': dmarc_pass,
        'failed':     dmarc_fail,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_dmarc_analytics.html', {
        **summary,
        'preset':         preset,
        'date_from':      str(date_from),
        'date_to':        str(date_to),
        'policy_filter':  policy_filter,
        'result_filter':  result_filter,
        'table_rows':     table_rows,
        'chart_data_json': json.dumps(chart_data),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Header Analysis Analytics
# ─────────────────────────────────────────────────────────────────────────────

def header_analysis_analytics(request):
    if _login_required(request):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    date_from, date_to, preset = _parse_date_range(request)
    is_ajax = request.GET.get('format') == 'json'

    qs = EmailHeader.objects.filter(
        user_id=user_id,
        created_at__date__range=[date_from, date_to],
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    total       = qs.count()
    clean       = qs.filter(risk_level='SAFE').count()
    issues_found = qs.filter(risk_level='RISKY').count()
    spam_risk   = qs.filter(risk_level='DANGEROUS').count()
    spf_pass    = qs.filter(spf_status='pass').count()
    dkim_pass   = qs.filter(dkim_status='pass').count()

    # ── Chart 2: Line — daily header checks ──────────────────────────────────
    daily_qs = (
        qs.annotate(day=TruncDate('created_at'))
        .values('day').annotate(cnt=Count('id')).order_by('day')
    )
    trend_labels = [str(r['day']) for r in daily_qs]
    trend_data   = [r['cnt'] for r in daily_qs]

    # ── Chart 3: Stacked — daily risk breakdown ───────────────────────────────
    risk_qs = (
        qs.annotate(day=TruncDate('created_at'))
        .values('day', 'risk_level')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )
    risk_map = defaultdict(lambda: {'SAFE': 0, 'RISKY': 0, 'DANGEROUS': 0})
    for r in risk_qs:
        risk_map[str(r['day'])][r['risk_level']] = r['cnt']
    risk_labels = sorted(risk_map.keys())
    issues_trend = {
        'labels': risk_labels,
        'spam':   [risk_map[d]['DANGEROUS'] for d in risk_labels],
        'auth':   [risk_map[d]['RISKY']     for d in risk_labels],
        'format': [0] * len(risk_labels),
        'security': [0] * len(risk_labels),
    }

    # ── Table ─────────────────────────────────────────────────────────────────
    table_qs = qs.values(
        'from_email', 'origin_ip', 'subject', 'spf_status', 'dkim_status', 'dmarc_status', 'risk_level', 'created_at'
    ).order_by('-created_at')[:20]
    table_rows = [
        {
            'email':   r['from_email'] or '—',
            'ip':      r['origin_ip']  or '—',
            'subject': (r['subject'] or '—')[:60],
            'spf':     r['spf_status'],
            'dkim':    r['dkim_status'],
            'dmarc':   r['dmarc_status'],
            'risk':    r['risk_level'],
            'date':    r['created_at'].strftime('%b %d, %Y') if r['created_at'] else '—',
        }
        for r in table_qs
    ]

    chart_data = {
        'distribution': {'clean': clean, 'warning': issues_found, 'failed': spam_risk},
        'trend':        {'labels': trend_labels, 'data': trend_data},
        'auth':         {'spf': spf_pass, 'dkim': dkim_pass, 'dmarc': 0},
        'issues':       issues_trend,
    }
    summary = {
        'total':        total,
        'clean':        clean,
        'issues_found': issues_found,
        'spam_risk':    spam_risk,
        'spf_pass':     spf_pass,
        'dkim_pass':    dkim_pass,
    }

    if is_ajax:
        return JsonResponse({'summary': summary, 'charts': chart_data})

    return render(request, 'i_header_analysis_analytics.html', {
        **summary,
        'preset':     preset,
        'date_from':  str(date_from),
        'date_to':    str(date_to),
        'table_rows': table_rows,
        'chart_data_json': json.dumps(chart_data),
    })
