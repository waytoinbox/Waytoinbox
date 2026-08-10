"""
segments.py — CRUD views for Segment management.
"""

import csv
import json
import logging

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)

_MAX_CONDITIONS = 20

_NO_VALUE_OPS = {
    'is_empty', 'is_not_empty', 'today', 'yesterday',
    'opened_any', 'not_opened_any', 'clicked_any', 'not_clicked_any',
    'delivered', 'bounced', 'complained', 'rejected',
}


def _require_login(request):
    return not request.session.get('logged_in')


def _parse_body(request):
    try:
        return json.loads(request.body)
    except (ValueError, TypeError):
        return {}


def _segment_to_dict(seg, user_id, include_count=True):
    from Email_validate_app.services.segment_builder import count_segment_contacts
    d = {
        'id':          seg.id,
        'name':        seg.name,
        'description': seg.description,
        'status':      seg.status,
        'match_type':  seg.match_type,
        'rules':       seg.rules,
        'created_at':  seg.created_at.strftime('%d %b %Y'),
        'updated_at':  seg.updated_at.strftime('%d %b %Y'),
    }
    if include_count:
        try:
            d['contact_count'] = count_segment_contacts(seg, user_id)
        except Exception:
            d['contact_count'] = 0
    return d


def _validate_condition(c, label):
    """Validate one condition dict. Returns error string or None."""
    if not isinstance(c, dict):
        return f'{label} is invalid.'
    if not c.get('field'):
        return f'{label}: field is required.'
    if not c.get('operator'):
        return f'{label}: operator is required.'
    if c.get('operator') not in _NO_VALUE_OPS:
        if c.get('value', '') == '':
            return f'{label}: value is required for operator "{c["operator"]}".'
    return None


def _validate_rules(rules):
    """Returns (rules dict, error string or None). Accepts grouped and flat formats."""
    if not isinstance(rules, dict):
        return None, 'rules must be a JSON object'

    # ── Per-connector grouped format ──────────────────────────────────────
    if 'groups' in rules:
        groups = rules.get('groups', [])
        if not isinstance(groups, list) or len(groups) == 0:
            return None, 'At least one condition group is required.'
        total = 0
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                return None, f'Group {gi+1} is invalid.'
            if gi > 0:
                connector = group.get('connector', 'and')
                if connector not in ('and', 'or'):
                    return None, f'Group {gi+1}: connector must be "and" or "or".'
            conditions = group.get('conditions', [])
            if not isinstance(conditions, list) or len(conditions) == 0:
                return None, f'Group {gi+1} must have at least one condition.'
            total += len(conditions)
            if total > _MAX_CONDITIONS:
                return None, f'Maximum {_MAX_CONDITIONS} total conditions allowed.'
            for i, c in enumerate(conditions):
                err = _validate_condition(c, f'Group {gi+1}, condition {i+1}')
                if err:
                    return None, err
        return rules, None

    # ── Legacy flat format ────────────────────────────────────────────────
    conditions = rules.get('conditions', [])
    if not isinstance(conditions, list) or len(conditions) == 0:
        return None, 'At least one condition is required.'
    if len(conditions) > _MAX_CONDITIONS:
        return None, f'Maximum {_MAX_CONDITIONS} conditions allowed.'
    for i, c in enumerate(conditions):
        err = _validate_condition(c, f'Condition {i+1}')
        if err:
            return None, err
    return rules, None


# ── Page views ────────────────────────────────────────────────────────────────

def segments_list(request):
    """Render the Segments list page."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import Segment
    from Email_validate_app.services.segment_builder import count_segment_contacts

    user_id  = get_user_id(request)
    segments = Segment.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-created_at')

    seg_data = []
    for seg in segments:
        try:
            count = count_segment_contacts(seg, user_id)
        except Exception:
            count = 0
        seg_data.append({
            'id':           seg.id,
            'name':         seg.name,
            'description':  seg.description,
            'status':       seg.status,
            'match_type':   seg.match_type,
            'contact_count': count,
            'created_at':   seg.created_at,
            'updated_at':   seg.updated_at,
        })

    return render(request, 'i_Segments.html', {
        'segments':    seg_data,
        'total':       len(seg_data),
        'pf_statuses': [('active', 'Active'), ('inactive', 'Inactive')],
    })


def segment_builder(request):
    """Render the Segment Builder page (create mode)."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import CampaignList, Campaign
    user_id = get_user_id(request)
    lists    = CampaignList.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('list_name')
    campaigns = Campaign.objects.filter(user_id=user_id, deleted_at__isnull=True).exclude(status='draft').order_by('-created_at')

    return render(request, 'i_Segment_Builder.html', {
        'lists':     list(lists.values('id', 'list_name', 'total_count')),
        'campaigns': list(campaigns.values('id', 'Campaign_ID', 'campaign_name')),
        'editing':   False,
        'segment':   None,
    })


def segment_builder_edit(request, seg_id):
    """Render the Segment Builder page (edit mode)."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import Segment, CampaignList, Campaign
    user_id = get_user_id(request)

    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        messages.error(request, 'Segment not found.')
        return redirect(reverse('segments'))

    lists    = CampaignList.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('list_name')
    campaigns = Campaign.objects.filter(user_id=user_id, deleted_at__isnull=True).exclude(status='draft').order_by('-created_at')

    return render(request, 'i_Segment_Builder.html', {
        'lists':     list(lists.values('id', 'list_name', 'total_count')),
        'campaigns': list(campaigns.values('id', 'Campaign_ID', 'campaign_name')),
        'editing':   True,
        'segment':   {
            'id':          seg.id,
            'name':        seg.name,
            'description': seg.description,
            'status':      seg.status,
            'match_type':  seg.match_type,
            'rules':       seg.rules,
        },
    })


# ── API views ─────────────────────────────────────────────────────────────────

def segment_api(request):
    """GET → list segments JSON. POST → create segment."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import Segment
    user_id = get_user_id(request)

    if request.method == 'GET':
        segments = Segment.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-created_at')
        return JsonResponse({'status': 'ok', 'segments': [_segment_to_dict(s, user_id) for s in segments]})

    if request.method == 'POST':
        body       = _parse_body(request)
        name       = (body.get('name') or '').strip()
        description = (body.get('description') or '').strip()
        status     = body.get('status', 'active')
        match_type = body.get('match_type', 'all')
        rules      = body.get('rules', {})

        if not name:
            return JsonResponse({'status': 'error', 'message': 'Segment name is required.'}, status=400)
        if len(name) > 255:
            return JsonResponse({'status': 'error', 'message': 'Name must be 255 characters or less.'}, status=400)
        if status not in ('active', 'inactive'):
            status = 'active'
        if match_type not in ('all', 'any'):
            match_type = 'all'

        # Name uniqueness (case-insensitive, per user, non-deleted)
        if Segment.objects.filter(user_id=user_id, name__iexact=name, deleted_at__isnull=True).exists():
            return JsonResponse({'status': 'error', 'message': f'A segment named "{name}" already exists.'}, status=400)

        _, err = _validate_rules(rules)
        if err:
            return JsonResponse({'status': 'error', 'message': err}, status=400)

        seg = Segment.objects.create(
            user_id=user_id,
            name=name,
            description=description,
            status=status,
            match_type=match_type,
            rules=rules,
        )
        return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(seg, user_id)}, status=201)

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


def segment_detail_api(request, seg_id):
    """GET → single segment. PUT → update. DELETE → soft-delete."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import Segment, Campaign
    user_id = get_user_id(request)

    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Segment not found.'}, status=404)

    if request.method == 'GET':
        return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(seg, user_id)})

    if request.method in ('PUT', 'PATCH'):
        body       = _parse_body(request)
        name       = (body.get('name') or seg.name).strip()
        description = (body.get('description', seg.description) or '').strip()
        status     = body.get('status', seg.status)
        match_type = body.get('match_type', seg.match_type)
        rules      = body.get('rules', seg.rules)

        if not name:
            return JsonResponse({'status': 'error', 'message': 'Segment name is required.'}, status=400)
        if status not in ('active', 'inactive'):
            status = seg.status
        if match_type not in ('all', 'any'):
            match_type = seg.match_type

        # Name uniqueness check (excluding self)
        dup = Segment.objects.filter(user_id=user_id, name__iexact=name, deleted_at__isnull=True).exclude(id=seg_id)
        if dup.exists():
            return JsonResponse({'status': 'error', 'message': f'A segment named "{name}" already exists.'}, status=400)

        _, err = _validate_rules(rules)
        if err:
            return JsonResponse({'status': 'error', 'message': err}, status=400)

        seg.name        = name
        seg.description = description
        seg.status      = status
        seg.match_type  = match_type
        seg.rules       = rules
        seg.save(update_fields=['name', 'description', 'status', 'match_type', 'rules', 'updated_at'])
        return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(seg, user_id)})

    if request.method == 'DELETE':
        # Block delete if used by a non-draft campaign
        active_campaigns = Campaign.objects.filter(
            campaign_segment=seg, deleted_at__isnull=True
        ).exclude(status='draft')
        if active_campaigns.exists():
            names = ', '.join(c.campaign_name for c in active_campaigns[:3])
            return JsonResponse({
                'status': 'error',
                'message': f'This segment is used by active campaign(s): {names}. Archive the campaign(s) first.',
            }, status=409)

        seg.deleted_at = timezone.now()
        seg.save(update_fields=['deleted_at'])
        return JsonResponse({'status': 'ok', 'message': f'"{seg.name}" deleted.'})

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


def segment_duplicate(request, seg_id):
    """POST → duplicate a segment."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import Segment
    user_id = get_user_id(request)

    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Segment not found.'}, status=404)

    # Find a unique name for the copy
    base_name = f'Copy of {seg.name}'[:255]
    new_name  = base_name
    counter   = 2
    while Segment.objects.filter(user_id=user_id, name__iexact=new_name, deleted_at__isnull=True).exists():
        new_name = f'{base_name} ({counter})'[:255]
        counter += 1

    copy = Segment.objects.create(
        user_id=user_id,
        name=new_name,
        description=seg.description,
        status='inactive',  # copies start as inactive
        match_type=seg.match_type,
        rules=seg.rules,
    )
    return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(copy, user_id)}, status=201)


def segment_preview(request):
    """POST → return count + sample contacts for a set of rules (unsaved or saved)."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.services.segment_builder import get_segment_preview

    user_id    = get_user_id(request)
    body       = _parse_body(request)
    rules      = body.get('rules', {})
    match_type = body.get('match_type', 'all')

    if match_type not in ('all', 'any'):
        match_type = 'all'

    try:
        result = get_segment_preview(rules, user_id, match_type=match_type, limit=15)
        return JsonResponse({'status': 'ok', **result})
    except Exception as exc:
        logger.error('segment_preview error: %s', exc)
        return JsonResponse({'status': 'error', 'message': 'Preview failed. Check your conditions.'}, status=400)


def segment_contacts(request, seg_id):
    """Render the full contacts page for a segment."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))
    from Email_validate_app.models import Segment
    user_id = get_user_id(request)
    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        messages.error(request, 'Segment not found.')
        return redirect(reverse('segments'))
    return render(request, 'i_Segment_Contacts.html', {'segment': seg})


def segment_contacts_page(request, seg_id):
    """AJAX: return a page of matching contacts as JSON."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    from Email_validate_app.models import Segment
    from Email_validate_app.services.segment_builder import build_segment_queryset
    user_id = get_user_id(request)
    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Not found'}, status=404)

    from django.db.models import Q as DQ
    q    = request.GET.get('q', '').strip()
    page = max(1, int(request.GET.get('page', 1) or 1))

    base = build_segment_queryset(seg, user_id).distinct()
    if q:
        base = base.filter(
            DQ(email__icontains=q) | DQ(first_name__icontains=q) | DQ(last_name__icontains=q)
        )
    qs = base.values(
        'email', 'first_name', 'last_name', 'subscribed', 'created_at'
    ).order_by('email')

    paginator = Paginator(qs, 20)
    pg        = paginator.get_page(page)
    _sub_map = {'1': 'subscribed', '0': 'never_subscribed', 'true': 'subscribed', 'false': 'never_subscribed'}
    contacts  = [
        {
            'email':      r['email'],
            'first_name': r['first_name'] or '',
            'last_name':  r['last_name'] or '',
            'subscribed': _sub_map.get(str(r['subscribed']).lower(), r['subscribed']),
            'date_added': r['created_at'].strftime('%d %b %Y') if r['created_at'] else '',
        }
        for r in pg.object_list
    ]
    return JsonResponse({
        'status':    'ok',
        'contacts':  contacts,
        'total':     paginator.count,
        'page':      pg.number,
        'pages':     paginator.num_pages,
        'has_next':  pg.has_next(),
        'has_prev':  pg.has_previous(),
    })


def segment_download(request, seg_id):
    """Stream matching contacts as a CSV file."""
    if _require_login(request):
        return redirect(reverse('login'))
    from Email_validate_app.models import Segment
    from Email_validate_app.services.segment_builder import build_segment_queryset
    user_id = get_user_id(request)
    try:
        seg = Segment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except Segment.DoesNotExist:
        return HttpResponse('Not found', status=404)

    qs       = build_segment_queryset(seg, user_id).values(
        'email', 'first_name', 'last_name', 'subscribed', 'created_at'
    ).distinct().order_by('email')
    safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in seg.name)[:50]
    response  = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="segment_{safe_name}_{seg_id}.csv"'
    writer = csv.writer(response)
    _sub_map = {'1': 'subscribed', '0': 'never_subscribed', 'true': 'subscribed', 'false': 'never_subscribed'}
    writer.writerow(['Email', 'First Name', 'Last Name', 'Status', 'Date Added'])
    for r in qs:
        writer.writerow([
            r['email'], r['first_name'] or '', r['last_name'] or '',
            _sub_map.get(str(r['subscribed']).lower(), r['subscribed']),
            r['created_at'].strftime('%Y-%m-%d') if r['created_at'] else '',
        ])
    return response
