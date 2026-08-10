import datetime
import logging

from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.db.models.signals import post_save
from django.utils import timezone

from Email_validate_app.models import (
    UserTable, CurrentCredits, SubsPayment, LoginActivity,
    Campaign, SenderDomain, SenderEmailToken,
    ListFiles, BlocklistMonitor, DomainBlocklist, EmailHeader,
    UsedCredits, Payment, Reputation, EmailValidate, DMARCAnalysis,
)

logger = logging.getLogger(__name__)

CACHE_VERSION   = 'v2'
CACHE_TTL       = 90   # seconds for dashboard summary
CACHE_TTL_CHART = 120  # seconds for chart data

QUICK_ACTIONS = [
    {'url': 'upload',            'icon': 'fa-envelopes-bulk',  'label': 'Bulk Verify',        'desc': 'Upload a list and validate at scale'},
    {'url': 'sender_verify',     'icon': 'fa-shield-alt',      'label': 'Verify Sender',      'desc': 'Authenticate a domain or email sender'},
    {'url': 'create_campaign',   'icon': 'fa-bullhorn',        'label': 'Create Campaign',    'desc': 'Design and send an email campaign'},
    {'url': 'Reputation_Analysis','icon': 'fa-chart-bar',      'label': 'Reputation Analysis','desc': 'Analyze DNS, DMARC, SPF, and DKIM records'},
    {'url': 'Blocklist_Monitor', 'icon': 'fa-server',          'label': 'IP Blocklist',       'desc': 'Check IPs against blocklists'},
    {'url': 'Domain_Blacklist',  'icon': 'fa-globe',           'label': 'Domain Blocklist',   'desc': 'Check domains against blocklists'},
]

_STATUS_CHIP = {
    'Processing': 'db-chip--pending',
    'Completed':  'db-chip--success',
    'Complete':   'db-chip--success',
    'sent':       'db-chip--success',
    'Sent':       'db-chip--success',
    'failed':     'db-chip--fail',
    'Failed':     'db-chip--fail',
    'cancelled':  'db-chip--warn',
    'Cancelled':  'db-chip--warn',
    'Stopped':    'db-chip--warn',
}


def _cache_key(user_id):
    return f'dashboard_{CACHE_VERSION}_{user_id}'


def _chart_cache_key(user_id, days):
    return f'dashboard_chart_{CACHE_VERSION}_{user_id}_{days}'


def invalidate_dashboard_cache(user_id):
    if not user_id:
        return
    cache.delete_many([
        _cache_key(user_id),
        _chart_cache_key(user_id, 30),
        _chart_cache_key(user_id, 90),
    ])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _chip(status):
    return _STATUS_CHIP.get(status or '', 'db-chip--pending')


def _get_user_info(user_id):
    user = UserTable.objects.filter(id=user_id).only('user_name').first()
    return {'username': user.user_name if user else 'User'}


def _get_credits(user_id):
    row = CurrentCredits.objects.filter(user_id=user_id).first()
    if not row:
        return {k: 0 for k in [
            'vc_current', 'vc_total', 'ac_current', 'ac_total', 'cc_current', 'cc_total'
        ]}
    return {
        'vc_current': row.vc_current_credits or 0,
        'vc_total':   row.vc_total_credits   or 0,
        'ac_current': row.ac_current_credits or 0,
        'ac_total':   row.ac_total_credits   or 0,
        'cc_current': row.cc_current_credits or 0,
        'cc_total':   row.cc_total_credits   or 0,
    }


def _get_subscription(user_id, now):
    sub = SubsPayment.objects.filter(
        user_id=user_id, plan_status='Active'
    ).order_by('-payment_time').first()
    renewal_days = None
    if sub and sub.valid_time:
        renewal_days = max(0, (sub.valid_time.date() - now.date()).days)
    return {
        'active_plan':  sub.subs_plan if sub else 'Free',
        'plan_status':  'Active' if sub else 'Free',
        'valid_time':   sub.valid_time if sub else None,
        'renewal_days': renewal_days,
    }


def _get_last_login(user_id):
    obj = LoginActivity.objects.filter(
        user_id=user_id, status='success'
    ).order_by('-login_at').first()
    return {'last_login': obj.login_at if obj else None}


def _get_campaign_summary(user_id):
    qs = Campaign.objects.filter(user_id=user_id, deleted_at__isnull=True)
    agg = qs.aggregate(
        total=Count('id'),
        active=Count('id',    filter=Q(status='sending')),
        scheduled=Count('id', filter=Q(status='scheduled')),
        failed=Count('id',    filter=Q(status='failed')),
        sent=Count('id',      filter=Q(status='sent')),
        draft=Count('id',     filter=Q(status='draft')),
    )
    last = qs.filter(status='sent').order_by('-sent_at').values('sent_at').first()
    return {
        '_campaign_qs':              qs,
        'campaign_count':            agg['total'],
        'active_campaign_count':     agg['active'],
        'scheduled_campaign_count':  agg['scheduled'],
        'failed_campaign_count':     agg['failed'],
        'draft_campaign_count':      agg['draft'],
        'sent_campaign_count':       agg['sent'],
        'last_campaign_date':        last['sent_at'] if last else None,
    }


def _get_sender_summary(user_id):
    dom = SenderDomain.objects.filter(
        user_id=user_id, deleted_at__isnull=True
    ).aggregate(
        pending=Count('id',  filter=Q(status='pending')),
        verified=Count('id', filter=Q(status='verified')),
    )
    em = SenderEmailToken.objects.filter(
        user_id=user_id, deleted_at__isnull=True, is_hidden=False
    ).aggregate(
        pending=Count('id',  filter=Q(confirmed=False)),
        verified=Count('id', filter=Q(confirmed=True)),
    )
    return {
        'pending_sender_count':  dom['pending']  + em['pending'],
        'verified_sender_count': dom['verified'] + em['verified'],
    }


def _get_blocklist_summary(user_id):
    ip_monitor_count   = BlocklistMonitor.objects.filter(user_id=user_id, is_hidden=False).count()
    domain_check_count = DomainBlocklist.objects.filter(user_id=user_id, is_hidden=False).count()
    ip_listed = (BlocklistMonitor.objects
                 .filter(user_id=user_id, is_hidden=False)
                 .exclude(listed_count__isnull=True)
                 .exclude(listed_count='')
                 .exclude(listed_count='0')
                 .count())
    dom_listed = (DomainBlocklist.objects
                  .filter(user_id=user_id, is_hidden=False)
                  .exclude(listed_count__isnull=True)
                  .exclude(listed_count='')
                  .exclude(listed_count='0')
                  .count())
    return {
        'ip_monitor_count':   ip_monitor_count,
        'domain_check_count': domain_check_count,
        'ip_listed':          ip_listed,
        'dom_listed':         dom_listed,
    }


def _get_week_stats(user_id, now):
    week_ago  = now - datetime.timedelta(days=7)
    veri_week = ListFiles.objects.filter(
        user_id=user_id, insert_date__gte=week_ago
    ).count()
    camp_week = Campaign.objects.filter(
        user_id=user_id, status='sent', sent_at__gte=week_ago, deleted_at__isnull=True
    ).count()
    return {
        'verifications_this_week': veri_week,
        'campaigns_sent_this_week': camp_week,
    }


def _get_extra_alert_data(user_id):
    failed_jobs = list(
        ListFiles.objects.filter(user_id=user_id, job_status__in=['Stopped', 'Failed'])
        .order_by('-insert_date')
        .values('file_id', 'table_name', 'file_name', 'job_status')[:5]
    )
    failed_campaigns = list(
        Campaign.objects.filter(user_id=user_id, status='failed', deleted_at__isnull=True)
        .order_by('-created_at').values('id', 'campaign_name')[:5]
    )
    draft_campaigns = list(
        Campaign.objects.filter(user_id=user_id, status='draft', deleted_at__isnull=True)
        .order_by('-created_at').values('id', 'campaign_name')[:5]
    )
    pending_domains = list(
        SenderDomain.objects.filter(user_id=user_id, status='pending', deleted_at__isnull=True)
        .values('id', 'domain')[:5]
    )
    pending_emails = list(
        SenderEmailToken.objects.filter(
            user_id=user_id, confirmed=False, deleted_at__isnull=True, is_hidden=False
        ).values('id', 'email')[:5]
    )
    unanalyzed_domains = list(
        SenderDomain.objects.filter(user_id=user_id, status='verified', deleted_at__isnull=True)
        .exclude(domain__in=Reputation.objects.filter(
            user_id=user_id, is_hidden=False, deleted_at__isnull=True
        ).values('domain'))
        .values('id', 'domain')[:5]
    )
    listed_ips = list(
        BlocklistMonitor.objects.filter(user_id=user_id, is_hidden=False)
        .exclude(listed_count__isnull=True).exclude(listed_count='').exclude(listed_count='0')
        .values('ip_id', 'ips')[:5]
    )
    listed_domains = list(
        DomainBlocklist.objects.filter(user_id=user_id, is_hidden=False)
        .exclude(listed_count__isnull=True).exclude(listed_count='').exclude(listed_count='0')
        .values('domain_id', 'domain')[:5]
    )
    return {
        'failed_jobs':        failed_jobs,
        'failed_campaigns':   failed_campaigns,
        'draft_campaigns':    draft_campaigns,
        'pending_domains':    pending_domains,
        'pending_emails':     pending_emails,
        'unanalyzed_domains': unanalyzed_domains,
        'listed_ips':         listed_ips,
        'listed_domains':     listed_domains,
    }


def _build_action_items(credits, sub, extra):
    items = []
    vc_pct = (credits["vc_current"] / credits["vc_total"] * 100) if credits.get("vc_total") else None
    ac_pct = (credits["ac_current"] / credits["ac_total"] * 100) if credits.get("ac_total") else None
    cc_pct = (credits["cc_current"] / credits["cc_total"] * 100) if credits.get("cc_total") else None
    renewal_days = sub.get("renewal_days")

    # Priority 1 - Critical (Red)
    for job in extra.get('failed_jobs', []):
        name   = job.get('table_name') or job.get('file_name') or ('Job #' + str(job['file_id']))
        status = job.get('job_status', 'Failed')
        items.append({
            'priority': 1, 'type': 'danger', 'icon': 'fa-envelopes-bulk',
            'service': 'Bulk Verify', 'reason': status,
            'msg': name,
            'cta': 'View', 'url': 'upload', 'pk': None,
        })
    for camp in extra.get('failed_campaigns', []):
        items.append({
            'priority': 1, 'type': 'danger', 'icon': 'fa-bullhorn',
            'service': 'Campaign', 'reason': 'Failed',
            'msg': camp['campaign_name'] or 'Unnamed Campaign',
            'cta': 'View', 'url': 'campaign_detail', 'pk': camp['id'],
        })
    for bl_ip in extra.get('listed_ips', []):
        items.append({
            'priority': 1, 'type': 'danger', 'icon': 'fa-server',
            'service': 'IP Blocklist', 'reason': 'Listed',
            'msg': bl_ip['ips'],
            'cta': 'View', 'url': 'Blocklist_Monitor', 'pk': None,
        })
    for bl_dom in extra.get('listed_domains', []):
        items.append({
            'priority': 1, 'type': 'danger', 'icon': 'fa-globe',
            'service': 'Domain Blocklist', 'reason': 'Listed',
            'msg': bl_dom['domain'],
            'cta': 'View', 'url': 'Domain_Blacklist', 'pk': None,
        })

    # Priority 2 - Warn (Orange)
    if renewal_days is not None and renewal_days <= 7:
        items.append({
            'priority': 2, 'type': 'warn', 'icon': 'fa-clock',
            'service': 'Subscription', 'reason': 'Expiring',
            'msg': 'Expires in ' + str(renewal_days) + (' days' if renewal_days != 1 else ' day'),
            'cta': 'Renew', 'url': 'subscription', 'pk': None,
        })
    if vc_pct is not None and vc_pct < 10:
        items.append({
            'priority': 2, 'type': 'warn', 'icon': 'fa-envelope-open-text',
            'service': 'Validation Credits', 'reason': 'Low',
            'msg': str(credits['vc_current']) + ' remaining (' + str(round(vc_pct)) + '%)',
            'cta': 'Buy', 'url': 'pricing', 'pk': None,
        })
    if ac_pct is not None and ac_pct < 10:
        items.append({
            'priority': 2, 'type': 'warn', 'icon': 'fa-chart-bar',
            'service': 'Analysis Credits', 'reason': 'Low',
            'msg': str(credits['ac_current']) + ' remaining (' + str(round(ac_pct)) + '%)',
            'cta': 'Buy', 'url': 'pricing', 'pk': None,
        })
    if cc_pct is not None and cc_pct < 10:
        items.append({
            'priority': 2, 'type': 'warn', 'icon': 'fa-paper-plane',
            'service': 'Contact Credits', 'reason': 'Low',
            'msg': str(credits['cc_current']) + ' remaining (' + str(round(cc_pct)) + '%)',
            'cta': 'Buy', 'url': 'pricing', 'pk': None,
        })

    # Priority 3 - Notice (Amber)
    for dom in extra.get('pending_domains', []):
        items.append({
            'priority': 3, 'type': 'notice', 'icon': 'fa-shield-alt',
            'service': 'Sender Domain', 'reason': 'Not Verified',
            'msg': dom['domain'],
            'cta': 'Verify', 'url': 'sender_verify', 'pk': None,
        })
    for em in extra.get('pending_emails', []):
        items.append({
            'priority': 3, 'type': 'notice', 'icon': 'fa-envelope',
            'service': 'Sender Email', 'reason': 'Not Verified',
            'msg': em['email'],
            'cta': 'Verify', 'url': 'sender_verify', 'pk': None,
        })
    for dom in extra.get('unanalyzed_domains', []):
        items.append({
            'priority': 3, 'type': 'notice', 'icon': 'fa-chart-bar',
            'service': 'Reputation Analysis', 'reason': 'Not Analyzed',
            'msg': dom['domain'],
            'cta': 'Analyze', 'url': 'Reputation_Analysis', 'pk': None,
        })

    # Priority 4 - Work (Warm brown)
    for camp in extra.get('draft_campaigns', []):
        items.append({
            'priority': 4, 'type': 'work', 'icon': 'fa-bullhorn',
            'service': 'Campaign', 'reason': 'Draft',
            'msg': camp['campaign_name'] or 'Unnamed Campaign',
            'cta': 'Resume', 'url': 'campaign_detail', 'pk': camp['id'],
        })

    return items

def _get_continue_items(user_id):
    jobs = list(
        ListFiles.objects.filter(user_id=user_id, job_status='Processing')
        .order_by('-insert_date').values('file_id', 'file_name')[:5]
    )
    return [
        {
            'priority': 5, 'type': 'work', 'icon': 'fa-envelopes-bulk',
            'service': 'Bulk Verify', 'reason': 'Processing',
            'msg': j['file_name'] or ('Job #' + str(j['file_id'])),
            'cta': 'View', 'url': 'upload', 'pk': None,
        }
        for j in jobs
    ]


def _get_recent_activity(user_id, campaigns):
    qs = campaigns.get('_campaign_qs')
    acts = []

    for j in ListFiles.objects.filter(user_id=user_id).only(
        'table_name', 'job_status', 'total_count', 'valid_count', 'invalid_count', 'unknown_count', 'insert_date'
    ).order_by('-insert_date')[:10]:
        total   = j.total_count   or 0
        valid   = j.valid_count   or 0
        invalid = j.invalid_count or 0
        unknown = j.unknown_count or 0
        acts.append({
            'service': 'Bulk Verify', 'icon': 'fa-envelopes-bulk',
            'status': j.job_status or 'Unknown', 'chip': _chip(j.job_status),
            'summary': j.table_name or '—',
            'detail':  f'{total:,} total · {valid:,} valid · {invalid:,} invalid · {unknown:,} unknown',
            'time': j.insert_date,
        })

    if qs:
        for c in qs.filter(
            status__in=['sent', 'failed', 'cancelled']
        ).only('status', 'campaign_name', 'total_recipients', 'sent_at').order_by('-sent_at')[:10]:
            acts.append({
                'service': 'Campaign', 'icon': 'fa-bullhorn',
                'status': c.status.capitalize(), 'chip': _chip(c.status),
                'summary': c.campaign_name,
                'detail':  f'{c.total_recipients or 0:,} recipients',
                'time': c.sent_at,
            })

    for r in Reputation.objects.filter(
        user_id=user_id, is_hidden=False, deleted_at__isnull=True
    ).only('domain', 'status', 'created_at').order_by('-created_at')[:10]:
        acts.append({
            'service': 'Reputation Analysis', 'icon': 'fa-chart-bar',
            'status': r.status.capitalize() if r.status else 'Analyzed',
            'chip': 'db-chip--success' if r.status and r.status.lower() == 'verified' else 'db-chip--pending',
            'summary': r.domain,
            'detail':  'Domain reputation check',
            'time': r.created_at,
        })

    for b in BlocklistMonitor.objects.filter(
        user_id=user_id, is_hidden=False
    ).only('ips', 'listed_count', 'created_date').order_by('-created_date')[:10]:
        listed = b.listed_count and b.listed_count not in ('', '0')
        acts.append({
            'service': 'IP Blocklist', 'icon': 'fa-server',
            'status': 'Listed' if listed else 'Clean',
            'chip': 'db-chip--fail' if listed else 'db-chip--success',
            'summary': b.ips or '—',
            'detail':  f'Listed on {b.listed_count} blocklist(s)' if listed else 'Not on any blocklist',
            'time': b.created_date,
        })

    for d in DomainBlocklist.objects.filter(
        user_id=user_id, is_hidden=False
    ).only('domain', 'listed_count', 'created_date').order_by('-created_date')[:10]:
        listed = d.listed_count and d.listed_count not in ('', '0')
        acts.append({
            'service': 'Domain Blocklist', 'icon': 'fa-globe',
            'status': 'Listed' if listed else 'Clean',
            'chip': 'db-chip--fail' if listed else 'db-chip--success',
            'summary': d.domain,
            'detail':  f'Listed on {d.listed_count} blocklist(s)' if listed else 'Not on any blocklist',
            'time': d.created_date,
        })

    min_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    acts.sort(key=lambda x: x['time'] if x['time'] else min_dt, reverse=True)
    return acts[:20]


def _get_account_health(action_items):
    types = {i['type'] for i in action_items}
    if 'danger' in types:
        return {
            'account_health_ok': False,
            'account_health_level': 'critical',
            'account_health_msg': 'Needs Attention',
        }
    if 'warn' in types:
        return {
            'account_health_ok': False,
            'account_health_level': 'warning',
            'account_health_msg': 'Review Recommended',
        }
    if 'notice' in types:
        return {
            'account_health_ok': False,
            'account_health_level': 'warning',
            'account_health_msg': 'Review Recommended',
        }
    return {
        'account_health_ok': True,
        'account_health_level': 'healthy',
        'account_health_msg': 'Healthy',
    }


def _get_onboarding_steps(campaigns, senders, week_stats):
    return [
        {
            'label': 'Verify your first email',
            'url': 'single_service',
            'done': week_stats['verifications_this_week'] > 0,
        },
        {
            'label': 'Verify your sender',
            'url': 'sender_verify',
            'done': (senders['verified_sender_count'] > 0
                     or senders['pending_sender_count'] > 0),
        },
        {
            'label': 'Upload your contacts',
            'url': 'all_contacts',
            'done': campaigns['campaign_count'] > 0,
        },
        {
            'label': 'Create a campaign',
            'url': 'create_campaign',
            'done': campaigns['campaign_count'] > 0,
        },
        {
            'label': 'Send your first campaign',
            'url': 'campaigns',
            'done': campaigns['sent_campaign_count'] > 0,
        },
    ]


def _build_summary_cards(credits, campaigns):
    def _pct(cur, tot):
        return round(cur / tot * 100, 1) if tot else None

    # Credits Status — 3 bars, each colored by criticality level
    def _bar_color(pct):
        if pct is None:   return 'healthy'
        if pct < 10:      return 'critical'
        if pct < 30:      return 'warn'
        return 'healthy'

    credit_bars = [
        {
            'name': 'Validation',
            'pct':  _pct(credits['vc_current'], credits['vc_total']),
            'cur':  credits['vc_current'],
            'tot':  credits['vc_total'],
        },
        {
            'name': 'Analysis',
            'pct':  _pct(credits['ac_current'], credits['ac_total']),
            'cur':  credits['ac_current'],
            'tot':  credits['ac_total'],
        },
        {
            'name': 'Contact',
            'pct':  _pct(credits['cc_current'], credits['cc_total']),
            'cur':  credits['cc_current'],
            'tot':  credits['cc_total'],
        },
    ]
    for b in credit_bars:
        b['level'] = _bar_color(b['pct'])

    any_critical = any(b['level'] == 'critical' for b in credit_bars)
    any_warn     = any(b['level'] == 'warn'     for b in credit_bars)
    if any_critical:
        low_badge, low_badge_type = 'Critical', 'danger'
    elif any_warn:
        low_badge, low_badge_type = 'Low', 'danger'
    else:
        low_badge, low_badge_type = None, None

    low_card = {
        'label': 'Credits Status', 'icon': 'fa-triangle-exclamation',
        'value': None, 'total': None, 'pct': None,
        'url': 'pricing',
        'meta': None,
        'badge': low_badge, 'badge_type': low_badge_type,
        'bars': credit_bars,
    }

    return [
        {
            'label': 'Validation Credits', 'icon': 'fa-envelope-open-text',
            'value': credits['vc_current'], 'total': credits['vc_total'],
            'pct': _pct(credits['vc_current'], credits['vc_total']),
            'url': 'single_service', 'meta': f'{credits["vc_total"]:,} total',
            'badge': None, 'badge_type': None,
        },
        {
            'label': 'Analysis Credits', 'icon': 'fa-chart-bar',
            'value': credits['ac_current'], 'total': credits['ac_total'],
            'pct': _pct(credits['ac_current'], credits['ac_total']),
            'url': 'Reputation_Analysis', 'meta': f'{credits["ac_total"]:,} total',
            'badge': None, 'badge_type': None,
        },
        {
            'label': 'Contact Credits', 'icon': 'fa-paper-plane',
            'value': credits['cc_current'], 'total': credits['cc_total'],
            'pct': _pct(credits['cc_current'], credits['cc_total']),
            'url': 'campaigns', 'meta': f'{credits["cc_total"]:,} total',
            'badge': None, 'badge_type': None,
        },
        low_card,
    ]


# ── Entry Point ───────────────────────────────────────────────────────────────

def get_dashboard_context(user_id):
    key    = _cache_key(user_id)
    cached = cache.get(key)
    if cached:
        return cached

    now = timezone.now()

    user_info  = _get_user_info(user_id)
    credits    = _get_credits(user_id)
    sub        = _get_subscription(user_id, now)
    last_login = _get_last_login(user_id)
    campaigns  = _get_campaign_summary(user_id)
    senders    = _get_sender_summary(user_id)
    bl         = _get_blocklist_summary(user_id)
    week_stats = _get_week_stats(user_id, now)
    extra      = _get_extra_alert_data(user_id)

    alerts         = _build_action_items(credits, sub, extra)
    continue_items = _get_continue_items(user_id)
    all_action     = sorted(alerts + continue_items, key=lambda x: x['priority'])
    attention_count = len(all_action)

    recent_activity  = _get_recent_activity(user_id, campaigns)
    onboarding_steps = _get_onboarding_steps(campaigns, senders, week_stats)
    is_new_user      = not any(s['done'] for s in onboarding_steps)
    account_health   = _get_account_health(alerts)
    summary_cards    = _build_summary_cards(credits, campaigns)

    # Next scheduled campaign countdown
    _nc = (Campaign.objects
           .filter(user_id=user_id, status='scheduled',
                   schedule_at__gt=now, deleted_at__isnull=True)
           .order_by('schedule_at')
           .only('campaign_name', 'schedule_at', 'Campaign_ID')
           .first())
    next_campaign = {
        'name':        _nc.campaign_name,
        'schedule_at': _nc.schedule_at,
        'pk':          _nc.Campaign_ID,
    } if _nc else None

    # Legacy totals kept for compatibility with any template references
    bulk_total   = ListFiles.objects.filter(user_id=user_id).count()
    single_total = 0
    total_verifications = bulk_total + single_total
    header_count        = EmailHeader.objects.filter(user_id=user_id).count()

    ctx = {
        **user_info, **credits, **sub, **last_login, **campaigns, **senders, **bl,
        **week_stats, **account_health,
        'action_items':       all_action,
        'attention_count':    attention_count,
        'recent_activity':    recent_activity,
        'onboarding_steps':   onboarding_steps,
        'is_new_user':        is_new_user,
        'summary_cards':      summary_cards,
        'quick_actions':      QUICK_ACTIONS,
        'next_campaign':      next_campaign,
        'dashboard_loaded_at': now,
        # legacy
        'total_verifications': total_verifications,
        'ip_monitor_count':    bl['ip_monitor_count'],
        'header_count':        header_count,
    }

    cache.set(key, ctx, CACHE_TTL)
    return ctx


def get_chart_data(user_id, days=30):
    key    = _chart_cache_key(user_id, days)
    cached = cache.get(key)
    if cached:
        return cached

    now   = timezone.now()
    since = now - datetime.timedelta(days=days)
    date_range = [since + datetime.timedelta(days=i) for i in range(days + 1)]
    labels = [d.strftime('%Y-%m-%d') for d in date_range]

    def _daily(qs, date_field):
        mp = {
            row[0]: row[1]
            for row in qs.filter(**{f'{date_field}__date__gte': since.date()})
                         .annotate(day=TruncDate(date_field))
                         .values('day')
                         .annotate(n=Count('pk'))
                         .values_list('day', 'n')
        }
        return [mp.get(d.date(), 0) for d in date_range]

    data = {
        'labels': labels,
        'email_verify': _daily(
            EmailValidate.objects.filter(user_id=user_id, is_hidden=False), 'insert_date'
        ),
        'bulk_verify': _daily(
            ListFiles.objects.filter(user_id=user_id, job_status='Complete'), 'completed_at'
        ),
        'campaigns': _daily(
            Campaign.objects.filter(user_id=user_id, status='sent', deleted_at__isnull=True),
            'sent_at'
        ),
        'reputation': _daily(
            Reputation.objects.filter(user_id=user_id, is_hidden=False, deleted_at__isnull=True),
            'created_at'
        ),
        'headers': _daily(
            EmailHeader.objects.filter(user_id=user_id), 'created_at'
        ),
        'ip_blocklist': _daily(
            BlocklistMonitor.objects.filter(user_id=user_id, is_hidden=False), 'created_date'
        ),
        'domain_blocklist': _daily(
            DomainBlocklist.objects.filter(user_id=user_id, is_hidden=False), 'created_date'
        ),
        'dmarc': _daily(
            DMARCAnalysis.objects.filter(user_id=user_id, is_hidden=False), 'created_at'
        ),
    }
    cache.set(key, data, CACHE_TTL_CHART)
    return data


# ── Cache Invalidation Signals ────────────────────────────────────────────────

def _bust_cache(sender, instance, **kwargs):
    uid = getattr(instance, 'user_id', None)
    if uid is None:
        user_obj = getattr(instance, 'user', None)
        uid = getattr(user_obj, 'id', None)
    if uid:
        invalidate_dashboard_cache(uid)


for _model in (
    CurrentCredits, UsedCredits, SubsPayment, Payment,
    Campaign, SenderDomain, SenderEmailToken,
    BlocklistMonitor, DomainBlocklist,
):
    post_save.connect(_bust_cache, sender=_model, weak=False)
