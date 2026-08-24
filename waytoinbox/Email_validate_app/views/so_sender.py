import json
import logging
import smtplib
from datetime import datetime, time as dt_time

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.timezone import now
from django.views.decorators.http import require_POST

from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)

SEQ_MAX_STEPS    = 10
SEQ_MAX_VARIANTS = 4
VARIANT_LABELS   = 'ABCD'
SEQ_MAX_SUBSEQUENCES = 3
SUBSEQ_MAX_STEPS     = 5
SUBSEQ_MAX_VARIANTS  = 4
WEEKDAY_ABBR     = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
SEND_WINDOW_UNRESTRICTED = (WEEKDAY_ABBR, dt_time(0, 0, 0), dt_time(23, 59, 59))

PERSONALIZATION_TAGS = [
    {'label': '{{first_name}}',      'value': '{{first_name}}'},
    {'label': '{{last_name}}',       'value': '{{last_name}}'},
    {'label': '{{full_name}}',       'value': '{{full_name}}'},
    {'label': '{{email}}',           'value': '{{email}}'},
    {'label': '{{company}}',         'value': '{{company}}'},
    {'label': '{{phone}}',           'value': '{{phone}}'},
    {'label': '{{unsubscribe_url}}', 'value': '{{unsubscribe_url}}'},
]


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def _auth_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)


# ── Campaign list ──────────────────────────────────────────────────────────────

def so_campaigns(request):
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOCampaign
    user_id = get_user_id(request)
    qs = SOCampaign.objects.filter(user_id=user_id, deleted_at__isnull=True).annotate(
        recipient_count=Count('campaign_contacts', distinct=True),
    ).order_by('-created_at')

    search    = request.GET.get('q', '').strip()
    status    = request.GET.get('status', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to', '').strip()
    if search:
        qs = qs.filter(name__icontains=search)
    if status:
        qs = qs.filter(status=status)

    def _parse_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    parsed_from = _parse_date(date_from)
    parsed_to   = _parse_date(date_to)
    if parsed_from:
        qs = qs.filter(created_at__date__gte=parsed_from)
    if parsed_to:
        qs = qs.filter(created_at__date__lte=parsed_to)

    try:
        page_size = int(request.GET.get('page_size') or 5)
    except (TypeError, ValueError):
        page_size = 5
    page_size = max(1, min(100, page_size))

    total     = qs.count()
    paginator = Paginator(qs, page_size)
    page_obj  = paginator.get_page(request.GET.get('page', 1))
    return render(request, 'i_SO_Sender.html', {
        'page_obj': page_obj, 'search': search, 'status_filter': status, 'total': total,
        'date_from': date_from, 'date_to': date_to,
        'page_size': page_size,
    })


# ── New Campaign page ──────────────────────────────────────────────────────────

def _serialize_sequence(campaign):
    """Nested steps -> variants, for hydrating the builder."""
    out = []
    for st in campaign.steps.prefetch_related('variants').order_by('order'):
        out.append({
            'id':         st.id,
            'order':      st.order,
            'wait_days':  st.wait_days,
            'wait_hours': st.wait_hours,
            'variants': [
                {
                    'id':        v.id,
                    'label':     v.label,
                    'name':      v.name,
                    'subject':   v.subject,
                    'preheader': v.preheader,
                    'html_body': v.html_body,
                    'weight':    v.weight,
                }
                for v in st.variants.all().order_by('label')
            ],
        })
    return out


def _serialize_subsequences(campaign):
    """Nested subsequences -> steps -> variants, for hydrating the builder.
    Reuses the same step/variant shape _serialize_sequence produces (just
    against SOSubsequenceStep/Variant instead), so the JS-side hydration code
    can treat both identically."""
    out = []
    for sub in campaign.subsequences.prefetch_related('steps__variants').order_by('order'):
        out.append({
            'id':           sub.id,
            'name':         sub.name,
            'order':        sub.order,
            'trigger_days': sub.trigger_days,
            'is_active':    sub.is_active,
            'steps': [
                {
                    'id':         st.id,
                    'order':      st.order,
                    'wait_days':  st.wait_days,
                    'wait_hours': st.wait_hours,
                    'variants': [
                        {
                            'id':        v.id,
                            'label':     v.label,
                            'name':      v.name,
                            'subject':   v.subject,
                            'preheader': v.preheader,
                            'html_body': v.html_body,
                            'weight':    v.weight,
                        }
                        for v in st.variants.all().order_by('label')
                    ],
                }
                for st in sub.steps.all().order_by('order')
            ],
        })
    return out


def _new_campaign_context(request, campaign=None):
    from Email_validate_app.models import SOEmailAccount, SOList, SOSegment
    from Email_validate_app.services.so_segment_builder import count_so_segment_prospects

    user_id  = get_user_id(request)
    accounts = SOEmailAccount.objects.filter(
        user_id=user_id, status='connected', deleted_at__isnull=True,
    ).order_by('email')
    lists = SOList.objects.filter(
        user_id=user_id, status='active', deleted_at__isnull=True,
    ).order_by('name')

    segments_data = []
    for seg in SOSegment.objects.filter(
        user_id=user_id, status='active', deleted_at__isnull=True,
    ).order_by('name'):
        try:
            cnt = count_so_segment_prospects(seg, user_id)
        except Exception:
            cnt = 0
        segments_data.append({'id': seg.id, 'name': seg.name, 'count': cnt})

    editing = None
    if campaign:
        editing = {
            'id':                   campaign.id,
            'status':               campaign.status,
            'name':                 campaign.name,
            'list_ids':             list(campaign.recipient_lists.values_list('id', flat=True)),
            'segment_ids':          list(campaign.recipient_segments.values_list('id', flat=True)),
            'exclude_list_ids':     list(campaign.exclude_lists.values_list('id', flat=True)),
            'exclude_segment_ids':  list(campaign.exclude_segments.values_list('id', flat=True)),
            'email_account_ids':    list(campaign.account_rotations.order_by('order')
                                         .values_list('account_id', flat=True)),
            'sender_name':          campaign.from_name,
            'reply_to':             campaign.reply_to,
            'schedule_at':          campaign.schedule_at.isoformat() if campaign.schedule_at else '',
            'schedule_timezone':    campaign.schedule_timezone or 'Asia/Kolkata',
            'send_option':          'schedule' if campaign.status == 'scheduled' else 'now',
            'send_window_enabled':  (
                campaign.send_weekdays != ','.join(SEND_WINDOW_UNRESTRICTED[0])
                or campaign.send_hour_start != SEND_WINDOW_UNRESTRICTED[1]
                or campaign.send_hour_end != SEND_WINDOW_UNRESTRICTED[2]
            ),
            'send_weekdays':        (campaign.send_weekdays or '').split(',') if campaign.send_weekdays else [],
            'send_hour_start':      campaign.send_hour_start.strftime('%H:%M'),
            'send_hour_end':        campaign.send_hour_end.strftime('%H:%M'),
            'sequence':             _serialize_sequence(campaign),
            'subsequences':         _serialize_subsequences(campaign),
        }

    return {
        'campaign':             campaign,
        'email_accounts':       accounts,
        'lists':                lists,
        'segments_data':        segments_data,
        'personalization_tags': PERSONALIZATION_TAGS,
        'editing_campaign':     editing,
    }


def so_campaign_create(request):
    r = _auth(request)
    if r:
        return r
    return render(request, 'i_SO_New_Campaign.html', _new_campaign_context(request))


def so_campaign_edit(request, cid):
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOCampaign
    user_id  = get_user_id(request)
    campaign = get_object_or_404(SOCampaign, id=cid, user_id=user_id, deleted_at__isnull=True)
    if campaign.status not in ('draft', 'scheduled', 'failed', 'cancelled'):
        return redirect(reverse('so_campaign_detail', args=[cid]) + '?notice=locked')
    return render(request, 'i_SO_New_Campaign.html', _new_campaign_context(request, campaign))


def so_campaign_detail(request, cid):
    r = _auth(request)
    if r:
        return r
    from datetime import datetime, time, timedelta, timezone as dt_timezone
    from Email_validate_app.models import SOCampaign, SOCampaignContact, SOEvent
    user_id  = get_user_id(request)
    campaign = get_object_or_404(
        SOCampaign.objects.prefetch_related(
            'account_rotations__account', 'recipient_lists', 'recipient_segments',
        ),
        id=cid, user_id=user_id, deleted_at__isnull=True,
    )
    contacts = SOCampaignContact.objects.filter(campaign=campaign).order_by('email')
    recipient_count = contacts.count()

    # ── Live sending progress ────────────────────────────────────────────────
    today_utc_start = datetime.combine(datetime.now(dt_timezone.utc).date(), time.min, tzinfo=dt_timezone.utc)
    contacts_sent_today = SOCampaignContact.objects.filter(
        campaign=campaign, sent_at__gte=today_utc_start,
    ).count()
    contacts_remaining = SOCampaignContact.objects.filter(
        campaign=campaign, status__in=('active', 'sending'),
    ).count()
    combined_daily_capacity = sum(
        r.account.daily_limit for r in campaign.account_rotations.select_related('account')
        if not r.account.deleted_at
    )

    # ── Per-recipient event journey (mirrors Email Marketing's campaign_detail:
    # one row per enrolled contact, event-presence flags built from SOEvent so
    # the same recipient can show Sent→Delivered→Opened→...→Replied at a glance).
    # Built from `contacts` (not just SOEvent) so a contact with zero events yet
    # — e.g. a later sequence step not due yet — still gets a row.
    _ORDERED_TYPES  = ['sent', 'delivered', 'opened', 'clicked', 'replied', 'bounced', 'complained', 'unsubscribed']
    _EVENT_PRIORITY = {et: i for i, et in enumerate(_ORDERED_TYPES)}

    events_qs = (
        SOEvent.objects.filter(campaign=campaign)
        .order_by('created_at')
        .values('email', 'event_type', 'created_at')
    )
    email_map = {}
    for ev in events_qs:
        addr = ev['email']
        entry = email_map.setdefault(addr, {'_last_event': None, '_last_time': None})
        entry[ev['event_type']] = True
        if entry['_last_time'] is None or ev['created_at'] > entry['_last_time']:
            entry['_last_event'] = ev['event_type']
            entry['_last_time'] = ev['created_at']

    recipient_rows = []
    for cc in contacts:
        data = email_map.get(cc.email, {})
        row = {
            'email':             cc.email,
            'cc_status':         cc.status,
            'cc_status_display': cc.get_status_display(),
            'sent_at':           cc.sent_at,
            'last_event':        data.get('_last_event'),
        }
        for et in _ORDERED_TYPES:
            row[et] = bool(data.get(et))
        recipient_rows.append(row)
    recipient_rows.sort(key=lambda r: _EVENT_PRIORITY.get(r['last_event'] or '', 99))

    def _pct(num, den):
        if not den:
            return None
        v = round(num / den * 100, 1)
        return int(v) if v == int(v) else v

    stat_pct = {
        'sent':         _pct(campaign.total_sent,         recipient_count),
        'delivered':    _pct(campaign.total_delivered,    campaign.total_sent),
        'opened':       _pct(campaign.total_opened,       campaign.total_delivered),
        'clicked':      _pct(campaign.total_clicked,      campaign.total_delivered),
        'replied':      _pct(campaign.total_replied,      campaign.total_delivered),
        'unsubscribed': _pct(campaign.total_unsubscribed, campaign.total_delivered),
        'bounced':      _pct(campaign.total_bounced,      campaign.total_sent),
        'complained':   _pct(campaign.total_complained,   campaign.total_sent),
        'failed':       _pct(campaign.total_failed,       recipient_count),
    }

    pf_statuses = [
        ('sent', 'Sent'), ('delivered', 'Delivered'), ('opened', 'Opened'),
        ('clicked', 'Clicked'), ('replied', 'Replied'), ('bounced', 'Bounced'),
        ('complained', 'Complained'), ('unsubscribed', 'Unsubscribed'),
    ]

    return render(request, 'i_SO_Campaign_Detail.html', {
        'campaign': campaign, 'recipient_count': recipient_count,
        'contacts_sent_today': contacts_sent_today,
        'contacts_remaining': contacts_remaining,
        'combined_daily_capacity': combined_daily_capacity,
        'recipient_rows': recipient_rows,
        'stat_pct': stat_pct,
        'pf_statuses': pf_statuses,
    })


# ── Sequence persistence ───────────────────────────────────────────────────────

def _validate_step_list(raw_steps, max_steps, max_variants, strict, error_key, prefix=''):
    """Shared step/variant cleaning — used for both the main sequence and each
    subsequence's own steps (identical structural rules; SOSequenceStep/
    SOSubsequenceStep are structural mirrors of each other). `prefix` (e.g.
    'Subsequence 2: ') distinguishes error messages when validating something
    other than the main sequence; the main sequence passes '' so its messages
    are unchanged from before this was factored out.

    Return (cleaned_steps, errors_dict). `strict` adds send-time requirements.
    """
    errors = {}
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, {error_key: f'{prefix}Add at least one step to the sequence.'}
    if len(raw_steps) > max_steps:
        return None, {error_key: f'{prefix}A sequence can have at most {max_steps} steps.'}

    cleaned = []
    for i, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            return None, {error_key: f'{prefix}Step {i + 1} is malformed.'}
        variants = raw_step.get('variants') or []
        if not isinstance(variants, list) or not variants:
            return None, {error_key: f'{prefix}Step {i + 1} needs at least one variation.'}
        if len(variants) > max_variants:
            return None, {error_key: f'{prefix}Step {i + 1} can have at most {max_variants} variations.'}

        try:
            wait_days  = max(0, min(90, int(raw_step.get('wait_days') or 0)))
            wait_hours = max(0, min(23, int(raw_step.get('wait_hours') or 0)))
        except (TypeError, ValueError):
            return None, {error_key: f'{prefix}Step {i + 1} has an invalid wait value.'}
        if i == 0:
            wait_days = wait_hours = 0          # step 1 always fires immediately

        cleaned_variants = []
        for j, raw_var in enumerate(variants):
            if not isinstance(raw_var, dict):
                return None, {error_key: f'{prefix}Step {i + 1} variation {j + 1} is malformed.'}
            subject = (raw_var.get('subject') or '').strip()[:500]
            body    = raw_var.get('html_body') or ''
            label   = (raw_var.get('label') or VARIANT_LABELS[j % max_variants])[:2]
            if strict:
                if not subject:
                    errors[error_key] = f'{prefix}Step {i + 1} variation {label} needs a subject line.'
                elif not body.strip():
                    errors[error_key] = f'{prefix}Step {i + 1} variation {label} needs an email body.'
            cleaned_variants.append({
                'id':        raw_var.get('id') or None,
                'client_id': raw_var.get('client_id') or '',
                'label':     label,
                'name':      (raw_var.get('name') or '').strip()[:255],
                'subject':   subject,
                'preheader': (raw_var.get('preheader') or '').strip()[:200],
                'html_body': body,
                'weight':    max(1, int(raw_var.get('weight') or 1)),
            })

        cleaned.append({
            'id':         raw_step.get('id') or None,
            'client_id':  raw_step.get('client_id') or '',
            'order':      i,
            'wait_days':  wait_days,
            'wait_hours': wait_hours,
            'variants':   cleaned_variants,
        })

    return cleaned, errors


def _validate_sequence(seq, strict):
    """Return (cleaned_steps, errors_dict). `strict` adds send-time requirements."""
    return _validate_step_list(seq, SEQ_MAX_STEPS, SEQ_MAX_VARIANTS, strict, 'sequence')


def _validate_subsequences(raw_subs, strict):
    """Return (cleaned_subsequences, errors_dict). Subsequences are optional —
    an absent/empty list is valid; not every campaign needs branching."""
    if not raw_subs:
        return [], {}
    if not isinstance(raw_subs, list):
        return None, {'subsequences': 'Subsequences payload is malformed.'}
    if len(raw_subs) > SEQ_MAX_SUBSEQUENCES:
        return None, {'subsequences': f'A campaign can have at most {SEQ_MAX_SUBSEQUENCES} subsequences.'}

    cleaned = []
    for k, raw_sub in enumerate(raw_subs):
        if not isinstance(raw_sub, dict):
            return None, {'subsequences': f'Subsequence {k + 1} is malformed.'}
        try:
            raw_trigger = raw_sub.get('trigger_days')
            # `raw_trigger or 3` would silently turn an explicit 0 into 3 (0 is
            # falsy) instead of clamping it to the floor of 1 — only fall back
            # to the default when the value is genuinely missing.
            trigger_days = max(1, min(30, int(raw_trigger if raw_trigger not in (None, '') else 3)))
        except (TypeError, ValueError):
            return None, {'subsequences': f'Subsequence {k + 1} has an invalid "no reply after" value.'}

        steps, step_errors = _validate_step_list(
            raw_sub.get('steps'), SUBSEQ_MAX_STEPS, SUBSEQ_MAX_VARIANTS, strict,
            'subsequences', prefix=f'Subsequence {k + 1}: ',
        )
        if step_errors:
            return None, step_errors

        cleaned.append({
            'id':           raw_sub.get('id') or None,
            'client_id':    raw_sub.get('client_id') or '',
            'name':         (raw_sub.get('name') or '').strip()[:255] or f'Subsequence {k + 1}',
            'order':        k,
            'trigger_type': 'no_reply',
            'trigger_days': trigger_days,
            'is_active':    bool(raw_sub.get('is_active', True)),
            'steps':        steps,
        })

    return cleaned, {}


def _sync_step_and_variant_rows(parent, related_name, step_model, variant_model, fk_field, specs):
    """Diff-sync a list of {id, client_id, order, wait_days, wait_hours,
    variants:[...]} specs against `getattr(parent, related_name)`. Shared by
    the main sequence (parent=campaign) and every subsequence (parent=a
    SOSubsequence instance) — SOSequenceStep/Variant and SOSubsequenceStep/
    Variant are structural mirrors, so the diffing logic is identical.
    Returns {client_id: db_id} covering both steps and variants, so autosave
    can keep updating rows instead of recreating them on every keystroke."""
    from Email_validate_app.services.so_html import sanitize_email_html

    step_qs = getattr(parent, related_name)
    id_map = {}
    existing_steps = {s.id: s for s in step_qs.all()}
    keep_step_ids = set()

    for spec in specs:
        step = existing_steps.get(spec['id']) if spec['id'] else None
        if step is None:
            step = step_model(**{fk_field: parent})
        step.order      = spec['order']
        step.wait_days  = spec['wait_days']
        step.wait_hours = spec['wait_hours']
        step.save()
        keep_step_ids.add(step.id)
        if spec['client_id']:
            id_map[spec['client_id']] = step.id

        existing_vars = {v.id: v for v in step.variants.all()}
        keep_var_ids = set()
        for vspec in spec['variants']:
            var = existing_vars.get(vspec['id']) if vspec['id'] else None
            if var is None:
                var = variant_model(step=step)
            var.label     = vspec['label']
            var.name      = vspec['name']
            var.subject   = vspec['subject']
            var.preheader = vspec['preheader']
            var.html_body = sanitize_email_html(vspec['html_body'])
            var.weight    = vspec['weight']
            var.save()
            keep_var_ids.add(var.id)
            if vspec['client_id']:
                id_map[vspec['client_id']] = var.id

        stale = set(existing_vars) - keep_var_ids
        if stale:
            variant_model.objects.filter(id__in=stale).delete()

    stale_steps = set(existing_steps) - keep_step_ids
    if stale_steps:
        step_model.objects.filter(id__in=stale_steps).delete()

    return id_map


def _sync_sequence(campaign, steps):
    """Diff-sync the main sequence's steps/variants. Returns {client_id: db_id}."""
    from Email_validate_app.models import SOSequenceStep, SOSequenceVariant

    return _sync_step_and_variant_rows(campaign, 'steps', SOSequenceStep, SOSequenceVariant, 'campaign', steps)


def _sync_subsequences(campaign, sub_specs):
    """Diff-sync SOSubsequence rows (by id) plus each one's steps/variants via
    the shared helper. Returns {client_id: db_id} covering subsequences and
    their steps/variants."""
    from Email_validate_app.models import SOSubsequence, SOSubsequenceStep, SOSubsequenceVariant

    id_map = {}
    existing = {s.id: s for s in campaign.subsequences.all()}
    keep_ids = set()

    for spec in sub_specs:
        sub = existing.get(spec['id']) if spec['id'] else None
        if sub is None:
            sub = SOSubsequence(campaign=campaign)
        sub.name         = spec['name']
        sub.order        = spec['order']
        sub.trigger_type = spec['trigger_type']
        sub.trigger_days = spec['trigger_days']
        sub.is_active    = spec['is_active']
        sub.save()
        keep_ids.add(sub.id)
        if spec['client_id']:
            id_map[spec['client_id']] = sub.id

        id_map.update(_sync_step_and_variant_rows(
            sub, 'steps', SOSubsequenceStep, SOSubsequenceVariant, 'subsequence', spec['steps'],
        ))

    stale = set(existing) - keep_ids
    if stale:
        SOSubsequence.objects.filter(id__in=stale).delete()

    return id_map


def _mirror_first_variant(campaign, steps):
    """Copy step 1 / variation A onto the legacy campaign columns.

    The existing one-shot sender reads SOCampaign.html_body, so this is what makes
    a sequence campaign sendable before the drip engine exists.
    """
    from Email_validate_app.services.so_html import sanitize_email_html
    if not steps or not steps[0]['variants']:
        return
    first = steps[0]['variants'][0]
    campaign.subject      = first['subject'][:500]
    campaign.preview_text = first['preheader'][:200]
    campaign.html_body    = sanitize_email_html(first['html_body'])


def _parse_schedule(date_str, time_str, tz_name):
    """('2026-09-01','09:30','Asia/Kolkata') -> aware UTC datetime. Raises ValueError."""
    from zoneinfo import ZoneInfo, available_timezones
    if tz_name not in available_timezones():
        tz_name = 'Asia/Kolkata'
    if not date_str or not time_str:
        raise ValueError('Pick a date and time for the scheduled send.')
    try:
        naive = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')
    except ValueError:
        raise ValueError('Schedule date or time is invalid.')
    aware = naive.replace(tzinfo=ZoneInfo(tz_name))
    if aware <= now():
        raise ValueError('Schedule time must be in the future.')
    return aware, tz_name


def _parse_send_window(data):
    """('send_window_enabled', 'send_weekdays'[], 'send_hour_start', 'send_hour_end')
    -> (weekdays_csv, start_time, end_time, error_or_None).

    When the toggle is off (or absent), returns the unrestricted sentinel
    regardless of whatever's in the other fields — same values a pre-this-
    feature campaign already has, so "off" truly means "no change in behavior."
    """
    if not data.get('send_window_enabled'):
        days, start, end = SEND_WINDOW_UNRESTRICTED
        return ','.join(days), start, end, None

    raw_days = data.get('send_weekdays') or []
    days = [d for d in WEEKDAY_ABBR if d in raw_days]   # keep mon..sun order
    if not days:
        return None, None, None, 'Select at least one sending day.'

    def _parse_hhmm(s, label):
        try:
            h, m = (s or '').split(':')
            h, m = int(h), int(m)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return dt_time(h, m)
        except (ValueError, TypeError):
            raise ValueError(f'{label} time is invalid.')

    try:
        start = _parse_hhmm(data.get('send_hour_start'), 'Start')
        end   = _parse_hhmm(data.get('send_hour_end'), 'End')
    except ValueError as exc:
        return None, None, None, str(exc)

    if start >= end:
        return None, None, None, 'Start time must be before end time.'

    return ','.join(days), start, end, None


def _apply_campaign_payload(request, data, strict):
    """Shared by save + autosave. Returns (campaign, id_map, errors)."""
    from Email_validate_app.models import (
        SOCampaign, SOEmailAccount, SOEmailAccountRotation, SOList, SOSegment,
    )

    user_id = get_user_id(request)
    errors  = {}

    name = (data.get('name') or '').strip()
    if not name:
        errors['name'] = 'Campaign name is required.'
    elif len(name) > 255:
        errors['name'] = 'Campaign name is too long.'

    list_ids     = [i for i in (data.get('recipient_list_ids') or []) if i]
    segment_ids  = [i for i in (data.get('recipient_segment_ids') or []) if i]
    excl_lists   = [i for i in (data.get('exclude_list_ids') or []) if i]
    excl_segs    = [i for i in (data.get('exclude_segment_ids') or []) if i]
    account_ids  = [i for i in (data.get('email_account_ids') or []) if i]

    steps, seq_errors = _validate_sequence(data.get('sequence'), strict)
    errors.update(seq_errors)
    subseqs, subseq_errors = _validate_subsequences(data.get('subsequences'), strict)
    errors.update(subseq_errors)

    action = data.get('action', 'save_draft')
    if strict:
        if not list_ids and not segment_ids:
            errors['recipients'] = 'Select at least one list or segment.'
        if not account_ids:
            errors['account'] = 'Select an email account to send from.'

    schedule_at = None
    schedule_tz = (data.get('schedule_timezone') or 'Asia/Kolkata').strip()
    if action == 'schedule':
        try:
            schedule_at, schedule_tz = _parse_schedule(
                data.get('schedule_date'), data.get('schedule_time'), schedule_tz,
            )
        except ValueError as exc:
            errors['schedule'] = str(exc)

    send_weekdays, send_hour_start, send_hour_end, window_error = _parse_send_window(data)
    if window_error:
        errors['send_window'] = window_error

    if errors or steps is None or subseqs is None:
        return None, {}, errors

    campaign_id = data.get('campaign_id') or 0
    if campaign_id:
        try:
            campaign = SOCampaign.objects.get(id=campaign_id, user_id=user_id, deleted_at__isnull=True)
        except SOCampaign.DoesNotExist:
            return None, {}, {'name': 'Campaign not found.'}
        if campaign.status not in ('draft', 'scheduled', 'failed', 'cancelled'):
            return None, {}, {'name': 'This campaign has already been sent and cannot be edited.'}
    else:
        campaign = SOCampaign(user_id=user_id, subject='', html_body='')

    campaign.name              = name
    campaign.send_mode         = 'sequence'
    campaign.from_name         = (data.get('sender_name') or '').strip()[:255]
    campaign.reply_to          = (data.get('reply_to') or '').strip()[:255]
    campaign.schedule_timezone = schedule_tz
    campaign.send_weekdays     = send_weekdays
    campaign.send_hour_start   = send_hour_start
    campaign.send_hour_end     = send_hour_end
    _mirror_first_variant(campaign, steps)

    if action == 'save_draft':
        campaign.status = 'draft'
    elif action == 'schedule':
        campaign.schedule_at = schedule_at
        campaign.status      = 'scheduled'
    elif action == 'send_now':
        campaign.status = 'sending'
    campaign.save()

    id_map = _sync_sequence(campaign, steps)
    id_map.update(_sync_subsequences(campaign, subseqs))

    campaign.recipient_lists.set(
        SOList.objects.filter(id__in=list_ids, user_id=user_id, deleted_at__isnull=True))
    campaign.recipient_segments.set(
        SOSegment.objects.filter(id__in=segment_ids, user_id=user_id, deleted_at__isnull=True))
    campaign.exclude_lists.set(
        SOList.objects.filter(id__in=excl_lists, user_id=user_id, deleted_at__isnull=True))
    campaign.exclude_segments.set(
        SOSegment.objects.filter(id__in=excl_segs, user_id=user_id, deleted_at__isnull=True))

    accounts = list(SOEmailAccount.objects.filter(
        id__in=account_ids, user_id=user_id, deleted_at__isnull=True))
    SOEmailAccountRotation.objects.filter(campaign=campaign).exclude(
        account__in=accounts).delete()
    for idx, acc in enumerate(accounts):
        SOEmailAccountRotation.objects.update_or_create(
            campaign=campaign, account=acc, defaults={'weight': 1, 'order': idx},
        )

    return campaign, id_map, {}


@require_POST
def so_campaign_save(request):
    r = _auth_json(request)
    if r:
        return r
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)

    action = data.get('action', 'save_draft')
    strict = action in ('schedule', 'send_now')

    with transaction.atomic():
        campaign, id_map, errors = _apply_campaign_payload(request, data, strict)
        if errors:
            return JsonResponse({'status': 'error', 'errors': errors}, status=400)

    if action == 'send_now':
        # Enqueue AFTER the transaction commits. If the broker is unreachable the
        # campaign must not be left stranded in a 'sending' state it will never
        # leave — roll it back to draft and say so plainly.
        from Email_validate_app.tasks.so_send_campaign import so_send_campaign_task
        try:
            so_send_campaign_task.delay(campaign.id)
        except Exception as exc:
            logger.exception('so_campaign_save: could not enqueue campaign %s', campaign.id)
            campaign.status = 'draft'
            campaign.save(update_fields=['status', 'updated_at'])
            return JsonResponse({
                'status': 'error',
                'errors': {'name': (
                    'Saved as a draft, but the send could not be queued because the '
                    'background task broker is unreachable ({}). Start Redis and the '
                    'Celery worker, then send again.'.format(type(exc).__name__)
                )},
                'campaign_id': campaign.id,
            }, status=503)

    return JsonResponse({
        'status':      'ok',
        'campaign_id': campaign.id,
        'action':      action,
        'id_map':      id_map,
        'saved_at':    now().isoformat(),
        'redirect':    reverse('so_campaigns') if action != 'save_draft' else '',
    })


@require_POST
def so_sequence_autosave(request):
    """Background draft save — never changes status, never sends."""
    r = _auth_json(request)
    if r:
        return r
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)

    data['action'] = 'save_draft'
    with transaction.atomic():
        campaign, id_map, errors = _apply_campaign_payload(request, data, strict=False)
        if errors:
            return JsonResponse({'status': 'error', 'errors': errors}, status=400)

    return JsonResponse({
        'status': 'ok', 'campaign_id': campaign.id,
        'id_map': id_map, 'saved_at': now().isoformat(),
    })


# ── Recipient estimate ─────────────────────────────────────────────────────────

@require_POST
def so_estimate_recipients(request):
    """Deduplicated count of subscribed prospects across lists/segments minus
    exclusions. Mirrors campaigns.estimate_recipients_api (form-encoded)."""
    r = _auth_json(request)
    if r:
        return r

    from Email_validate_app.models import SOListProspect, SOProspect, SOEvent
    from Email_validate_app.services.so_segment_builder import (
        build_so_segment_queryset,
    )
    from Email_validate_app.models import SOSegment

    user_id = get_user_id(request)

    def ids(field):
        raw = request.POST.get(field, '') or ''
        out = []
        for part in raw.split(','):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out

    def emails_for(list_ids, segment_ids):
        found = set()
        if list_ids:
            found.update(
                SOListProspect.objects.filter(
                    so_list_id__in=list_ids, so_list__user_id=user_id,
                    so_list__deleted_at__isnull=True,
                    prospect__deleted_at__isnull=True,
                    prospect__status='subscribed',
                ).values_list('prospect__email', flat=True)
            )
        if segment_ids:
            for seg in SOSegment.objects.filter(
                id__in=segment_ids, user_id=user_id, deleted_at__isnull=True,
            ):
                try:
                    found.update(
                        build_so_segment_queryset(seg, user_id)
                        .filter(status='subscribed')
                        .values_list('email', flat=True)
                    )
                except Exception:
                    continue
        return found

    # Match real enrollment's exclusions (tasks/so_send_campaign.py) so the
    # pre-launch estimate never overstates who will actually be enrolled.
    # Unsubscribed prospects are already excluded by emails_for's own
    # status='subscribed' filter above.
    suppressed = set(
        SOEvent.objects.filter(
            campaign__user_id=user_id, event_type__in=('bounced', 'complained'),
        ).values_list('email', flat=True)
    )

    included = emails_for(ids('list_ids'), ids('segment_ids'))
    excluded = emails_for(ids('exclude_list_ids'), ids('exclude_segment_ids'))
    return JsonResponse({'count': len(included - excluded - suppressed)})


# ── Content score ──────────────────────────────────────────────────────────────

@require_POST
def so_content_score(request):
    r = _auth_json(request)
    if r:
        return r
    from Email_validate_app.services.so_content_score import score_email
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)
    result = score_email(data.get('subject', ''), data.get('html_body', ''))
    return JsonResponse({'status': 'ok', **result})


# ── Per-campaign actions ───────────────────────────────────────────────────────

def so_campaign_action(request, cid):
    r = _auth(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOCampaign
    user_id = get_user_id(request)
    data    = json.loads(request.body)
    action  = data.get('action')

    try:
        campaign = SOCampaign.objects.get(id=cid, user_id=user_id, deleted_at__isnull=True)
    except SOCampaign.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Not found.'}, status=404)

    if action == 'cancel':
        if campaign.status in ('scheduled', 'paused', 'sending'):
            from Email_validate_app.models import SOCampaignContact
            campaign.status = 'cancelled'
            campaign.save(update_fields=['status', 'updated_at'])
            # A multi-step sequence can be mid-run for days — stop every
            # in-flight contact so the next dispatcher tick doesn't keep sending.
            SOCampaignContact.objects.filter(
                campaign_id=cid, status__in=('active', 'sending'),
            ).update(status='stopped', error='stopped: cancelled', next_action_at=None)
        return JsonResponse({'status': 'ok'})

    if action == 'pause':
        # Deliberately touches ONLY SOCampaign.status. The sequence dispatcher
        # (so_dispatch_due_sequence_steps) filters campaign__status='sending',
        # and so_dispatch_scheduled_campaigns filters status='scheduled' — so
        # flipping to 'paused' is sufficient on its own to stop every future
        # automated send, without touching a single SOCampaignContact row:
        # current_step, active_subsequence, next_action_at, and sent history
        # are all left exactly as they are.
        if campaign.status in ('scheduled', 'sending'):
            campaign.status = 'paused'
            campaign.save(update_fields=['status', 'updated_at'])
            return JsonResponse({'status': 'ok'})
        return JsonResponse({'status': 'error', 'message': 'Campaign cannot be paused from its current state.'})

    if action == 'resume':
        if campaign.status == 'paused':
            from Email_validate_app.models import SOCampaignContact
            # Restore whichever active state it was paused from. A campaign
            # paused before it ever launched (still 'scheduled', nothing
            # enrolled yet) goes back to 'scheduled' so the normal schedule
            # sweep resumes it; one paused mid-run (contacts already
            # enrolled) goes back to 'sending' so the sequence dispatcher
            # continues exactly where it left off.
            was_enrolled = SOCampaignContact.objects.filter(campaign_id=cid).exists()
            campaign.status = 'sending' if was_enrolled else 'scheduled'
            campaign.save(update_fields=['status', 'updated_at'])
            return JsonResponse({'status': 'ok'})
        return JsonResponse({'status': 'error', 'message': 'Campaign is not paused.'})

    if action == 'delete':
        if campaign.status in ('draft', 'failed', 'cancelled'):
            campaign.deleted_at = now()
            campaign.save(update_fields=['deleted_at'])
            return JsonResponse({'status': 'ok'})
        return JsonResponse({'status': 'error', 'message': 'Cannot delete a sent/sending campaign.'})

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})


# ── Test send ──────────────────────────────────────────────────────────────────

@require_POST
def so_test_send(request):
    """Send one draft variation to up to 5 addresses, immediately."""
    r = _auth_json(request)
    if r:
        return r

    from Email_validate_app.models import SOCampaign, SOEmailAccount
    from Email_validate_app.services.so_html import sanitize_email_html
    from Email_validate_app.services.so_smtp import build_message, open_smtp, substitute_tags

    data        = json.loads(request.body)
    user_id     = get_user_id(request)
    subject     = (data.get('subject') or '').strip()
    html_body   = sanitize_email_html(data.get('html_body') or '')
    account_id  = data.get('email_account_id')
    campaign_id = data.get('campaign_id')

    raw_to = data.get('to_emails')
    if raw_to is None:
        raw_to = [data.get('to_email') or '']          # back-compat, single address
    recipients = [e.strip() for e in raw_to if e and e.strip()]
    recipients = [e for e in recipients if '@' in e][:5]

    if not recipients:
        return JsonResponse({'status': 'error', 'message': 'Add at least one valid recipient.'})
    if not account_id:
        return JsonResponse({'status': 'error', 'message': 'Select an email account.'})
    if not subject:
        return JsonResponse({'status': 'error', 'message': 'This variation has no subject line yet.'})
    if not campaign_id:
        return JsonResponse({'status': 'error', 'message': 'Save the campaign before sending a test.'})

    try:
        campaign = SOCampaign.objects.get(id=campaign_id, user_id=user_id, deleted_at__isnull=True)
    except SOCampaign.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Campaign not found.'})

    try:
        account_id_int = int(account_id)
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid email account.'})

    # Never trust the client's account choice alone — it must actually be one
    # of THIS campaign's own selected sender accounts, not just any connected
    # account the requesting user happens to own.
    campaign_account_ids = set(campaign.account_rotations.values_list('account_id', flat=True))
    if account_id_int not in campaign_account_ids:
        return JsonResponse({
            'status': 'error',
            'message': 'That account is not one of this campaign\'s selected sender accounts.',
        })

    try:
        account = SOEmailAccount.objects.get(
            id=account_id_int, user_id=user_id, status='connected', deleted_at__isnull=True)
    except SOEmailAccount.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Email account not found or not connected.'})

    from_name = (data.get('sender_name') or '').strip() or account.display_name or account.email

    # Sample data for merge-tag rendering — a test send has no real enrolled
    # prospect to personalize against, so a preview needs to substitute
    # something rather than send the literal {{first_name}} placeholders.
    sample_data = {
        'first_name': 'John', 'last_name': 'Doe', 'company': 'Acme Inc',
        'phone': '', 'unsubscribe_url': '',
    }

    sent = 0
    try:
        server = open_smtp(account)
    except smtplib.SMTPAuthenticationError:
        return JsonResponse({'status': 'error', 'message': 'SMTP authentication failed.'})
    except Exception as exc:
        return JsonResponse({'status': 'error', 'message': f'Could not connect: {exc}'})

    try:
        for to_email in recipients:
            sample_data['email'] = to_email
            personalized_subject = substitute_tags(subject, sample_data)
            personalized_html    = substitute_tags(html_body, sample_data)
            msg = build_message(
                from_name, account.email, to_email, f'[TEST] {personalized_subject}',
                personalized_html, '', f'<test-{now().timestamp()}@{account.smtp_host}>',
            )
            server.sendmail(account.email, to_email, msg.as_bytes())
            sent += 1
    except Exception as exc:
        return JsonResponse({'status': 'error', 'message': f'Send failed after {sent}: {exc}'})
    finally:
        try:
            server.quit()
        except Exception:
            pass

    return JsonResponse({'status': 'ok', 'sent': sent})
