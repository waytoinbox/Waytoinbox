"""
so_segments.py — CRUD views for Sales Outreach segment management.
Mirrors views/segments.py, operating on SOSegment / SOProspect.
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

from Email_validate_app.utils import get_user_id

logger = logging.getLogger(__name__)

_MAX_CONDITIONS = 20

_NO_VALUE_OPS = {
    'is_empty', 'is_not_empty', 'today', 'yesterday',
    'opened_any', 'not_opened_any', 'clicked_any', 'not_clicked_any',
    'replied_any', 'not_replied_any',
    'delivered', 'bounced', 'complained', 'sent',
}


def _require_login(request):
    return not request.session.get('logged_in')


def _parse_body(request):
    try:
        return json.loads(request.body)
    except (ValueError, TypeError):
        return {}


def _segment_to_dict(seg, user_id, include_count=True):
    from Email_validate_app.services.so_segment_builder import count_so_segment_prospects
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
            d['prospect_count'] = count_so_segment_prospects(seg, user_id)
        except Exception:
            d['prospect_count'] = 0
    return d


def _validate_condition(c, label):
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

    if 'groups' in rules:
        groups = rules.get('groups', [])
        if not isinstance(groups, list) or len(groups) == 0:
            return None, 'At least one condition group is required.'
        total = 0
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                return None, f'Group {gi+1} is invalid.'
            if gi > 0 and group.get('connector', 'and') not in ('and', 'or'):
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

def so_segments_list(request):
    """Render the Sales Outreach Segments page."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import SOSegment
    from Email_validate_app.services.so_segment_builder import count_so_segment_prospects

    user_id  = get_user_id(request)
    segments = SOSegment.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-created_at')

    seg_data = []
    for seg in segments:
        try:
            count = count_so_segment_prospects(seg, user_id)
        except Exception:
            count = 0
        seg_data.append({
            'id':             seg.id,
            'name':           seg.name,
            'description':    seg.description,
            'status':         seg.status,
            'match_type':     seg.match_type,
            'prospect_count': count,
            'created_at':     seg.created_at,
            'updated_at':     seg.updated_at,
        })

    return render(request, 'i_SO_Segments.html', {
        'segments':    seg_data,
        'total':       len(seg_data),
        'pf_statuses': [('active', 'Active'), ('inactive', 'Inactive')],
    })


def _builder_context(request, seg=None):
    from Email_validate_app.models import SOList, SOCampaign
    user_id   = get_user_id(request)
    lists     = SOList.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('name')
    campaigns = SOCampaign.objects.filter(
        user_id=user_id, deleted_at__isnull=True,
    ).exclude(status='draft').order_by('-created_at')

    return {
        'lists':     list(lists.values('id', 'name', 'total_count')),
        'campaigns': list(campaigns.values('id', 'name')),
        'editing':   seg is not None,
        'segment':   {
            'id':          seg.id,
            'name':        seg.name,
            'description': seg.description,
            'status':      seg.status,
            'match_type':  seg.match_type,
            'rules':       seg.rules,
        } if seg else None,
    }


def so_segment_builder(request):
    """Render the Segment Builder page (create mode)."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))
    return render(request, 'i_SO_Segment_Builder.html', _builder_context(request))


def so_segment_builder_edit(request, seg_id):
    """Render the Segment Builder page (edit mode)."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import SOSegment
    user_id = get_user_id(request)
    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        messages.error(request, 'Segment not found.')
        return redirect(reverse('so_segments'))

    return render(request, 'i_SO_Segment_Builder.html', _builder_context(request, seg))


# ── API views ─────────────────────────────────────────────────────────────────

def so_segment_api(request):
    """GET → list segments JSON. POST → create segment."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import SOSegment
    user_id = get_user_id(request)

    if request.method == 'GET':
        segments = SOSegment.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-created_at')
        return JsonResponse({'status': 'ok', 'segments': [_segment_to_dict(s, user_id) for s in segments]})

    if request.method == 'POST':
        body        = _parse_body(request)
        name        = (body.get('name') or '').strip()
        description = (body.get('description') or '').strip()
        status      = body.get('status', 'active')
        match_type  = body.get('match_type', 'all')
        rules       = body.get('rules', {})

        if not name:
            return JsonResponse({'status': 'error', 'message': 'Segment name is required.'}, status=400)
        if len(name) > 255:
            return JsonResponse({'status': 'error', 'message': 'Name must be 255 characters or less.'}, status=400)
        if status not in ('active', 'inactive'):
            status = 'active'
        if match_type not in ('all', 'any'):
            match_type = 'all'

        if SOSegment.objects.filter(user_id=user_id, name__iexact=name, deleted_at__isnull=True).exists():
            return JsonResponse({'status': 'error', 'message': f'A segment named "{name}" already exists.'}, status=400)

        _, err = _validate_rules(rules)
        if err:
            return JsonResponse({'status': 'error', 'message': err}, status=400)

        seg = SOSegment.objects.create(
            user_id=user_id, name=name, description=description,
            status=status, match_type=match_type, rules=rules,
        )
        return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(seg, user_id)}, status=201)

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


def so_segment_detail_api(request, seg_id):
    """GET → single segment. PUT → update. DELETE → soft-delete."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import SOSegment, SOCampaign
    user_id = get_user_id(request)

    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Segment not found.'}, status=404)

    if request.method == 'GET':
        return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(seg, user_id)})

    if request.method in ('PUT', 'PATCH'):
        body        = _parse_body(request)
        name        = (body.get('name') or seg.name).strip()
        description = (body.get('description', seg.description) or '').strip()
        status      = body.get('status', seg.status)
        match_type  = body.get('match_type', seg.match_type)
        rules       = body.get('rules', seg.rules)

        if not name:
            return JsonResponse({'status': 'error', 'message': 'Segment name is required.'}, status=400)
        if status not in ('active', 'inactive'):
            status = seg.status
        if match_type not in ('all', 'any'):
            match_type = seg.match_type

        if SOSegment.objects.filter(
            user_id=user_id, name__iexact=name, deleted_at__isnull=True,
        ).exclude(id=seg_id).exists():
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
        active_campaigns = SOCampaign.objects.filter(
            recipient_segments=seg, deleted_at__isnull=True,
        ).exclude(status='draft')
        if active_campaigns.exists():
            names = ', '.join(c.name for c in active_campaigns[:3])
            return JsonResponse({
                'status': 'error',
                'message': f'This segment is used by active campaign(s): {names}. Archive the campaign(s) first.',
            }, status=409)

        seg.deleted_at = timezone.now()
        seg.save(update_fields=['deleted_at'])
        return JsonResponse({'status': 'ok', 'message': f'"{seg.name}" deleted.'})

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


def so_segment_duplicate(request, seg_id):
    """POST → duplicate a segment."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOSegment
    user_id = get_user_id(request)

    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Segment not found.'}, status=404)

    base_name = f'Copy of {seg.name}'[:255]
    new_name  = base_name
    counter   = 2
    while SOSegment.objects.filter(user_id=user_id, name__iexact=new_name, deleted_at__isnull=True).exists():
        new_name = f'{base_name} ({counter})'[:255]
        counter += 1

    copy = SOSegment.objects.create(
        user_id=user_id, name=new_name, description=seg.description,
        status='inactive',                 # copies start inactive
        match_type=seg.match_type, rules=seg.rules,
    )
    return JsonResponse({'status': 'ok', 'segment': _segment_to_dict(copy, user_id)}, status=201)


def so_segment_preview(request):
    """POST → count + sample prospects for a set of rules (saved or unsaved)."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.services.so_segment_builder import get_so_segment_preview

    user_id    = get_user_id(request)
    body       = _parse_body(request)
    rules      = body.get('rules', {})
    match_type = body.get('match_type', 'all')
    if match_type not in ('all', 'any'):
        match_type = 'all'

    try:
        result = get_so_segment_preview(rules, user_id, match_type=match_type, limit=15)
        return JsonResponse({'status': 'ok', **result})
    except Exception as exc:
        logger.error('so_segment_preview error: %s', exc)
        return JsonResponse({'status': 'error', 'message': 'Preview failed. Check your conditions.'}, status=400)


def so_segment_prospects(request, seg_id):
    """Render the full prospects page for a segment."""
    if _require_login(request):
        messages.warning(request, 'You need to login first.')
        return redirect(reverse('login'))

    from Email_validate_app.models import SOSegment
    user_id = get_user_id(request)
    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        messages.error(request, 'Segment not found.')
        return redirect(reverse('so_segments'))
    return render(request, 'i_SO_Segment_Prospects.html', {'segment': seg})


def so_segment_prospects_page(request, seg_id):
    """AJAX: return a page of matching prospects as JSON."""
    if _require_login(request):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from django.db.models import Q as DQ
    from Email_validate_app.models import SOSegment
    from Email_validate_app.services.so_segment_builder import build_so_segment_queryset

    user_id = get_user_id(request)
    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Not found'}, status=404)

    q    = request.GET.get('q', '').strip()
    page = max(1, int(request.GET.get('page', 1) or 1))

    base = build_so_segment_queryset(seg, user_id).distinct()
    if q:
        base = base.filter(
            DQ(email__icontains=q) | DQ(first_name__icontains=q) |
            DQ(last_name__icontains=q) | DQ(company__icontains=q)
        )
    qs = base.values('email', 'first_name', 'last_name', 'company', 'status', 'created_at').order_by('email')

    paginator = Paginator(qs, 20)
    pg        = paginator.get_page(page)
    prospects = [
        {
            'email':      r['email'],
            'first_name': r['first_name'] or '',
            'last_name':  r['last_name'] or '',
            'company':    r['company'] or '',
            'status':     r['status'],
            'date_added': r['created_at'].strftime('%d %b %Y') if r['created_at'] else '',
        }
        for r in pg.object_list
    ]
    return JsonResponse({
        'status':    'ok',
        'prospects': prospects,
        'total':     paginator.count,
        'page':      pg.number,
        'pages':     paginator.num_pages,
        'has_next':  pg.has_next(),
        'has_prev':  pg.has_previous(),
    })


def so_segment_download(request, seg_id):
    """Stream matching prospects as a CSV file."""
    if _require_login(request):
        return redirect(reverse('login'))

    from Email_validate_app.models import SOSegment
    from Email_validate_app.services.so_segment_builder import build_so_segment_queryset

    user_id = get_user_id(request)
    try:
        seg = SOSegment.objects.get(id=seg_id, user_id=user_id, deleted_at__isnull=True)
    except SOSegment.DoesNotExist:
        return HttpResponse('Not found', status=404)

    qs = build_so_segment_queryset(seg, user_id).values(
        'email', 'first_name', 'last_name', 'company', 'phone', 'status', 'created_at',
    ).distinct().order_by('email')

    safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in seg.name)[:50]
    response  = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="so_segment_{safe_name}_{seg_id}.csv"'

    writer = csv.writer(response)
    writer.writerow(['Email', 'First Name', 'Last Name', 'Company', 'Phone', 'Status', 'Date Added'])
    for r in qs:
        writer.writerow([
            r['email'], r['first_name'] or '', r['last_name'] or '',
            r['company'] or '', r['phone'] or '', r['status'],
            r['created_at'].strftime('%Y-%m-%d') if r['created_at'] else '',
        ])
    return response
