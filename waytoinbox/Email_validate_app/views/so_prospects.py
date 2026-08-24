import csv
import io
import json
from datetime import datetime

from django.core.paginator import Paginator
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.timezone import now

from Email_validate_app.utils import get_user_id


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def so_prospects(request):
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOProspect
    user_id  = get_user_id(request)
    base_qs  = SOProspect.objects.filter(user_id=user_id, deleted_at__isnull=True)
    qs       = base_qs.prefetch_related('prospect_lists__so_list')

    search    = request.GET.get('q', '').strip()
    status    = request.GET.get('status', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to', '').strip()
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(email__icontains=search) | Q(first_name__icontains=search) |
            Q(last_name__icontains=search) | Q(company__icontains=search)
        )
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

    paginator  = Paginator(qs, page_size)
    page_obj   = paginator.get_page(request.GET.get('page', 1))
    total      = qs.count()

    # Stat cards always reflect every prospect (ignoring search/status/date
    # filters), matching All Contacts' behavior — the counts are a stable
    # summary of the whole list, not a reflection of the current filter.
    raw_counts = {
        row['status']: row['cnt']
        for row in base_qs.values('status').annotate(cnt=Count('id'))
    }
    stats = {
        'total':            sum(raw_counts.values()),
        'subscribed':       raw_counts.get('subscribed', 0),
        'unsubscribed':     raw_counts.get('unsubscribed', 0),
        'never_subscribed': raw_counts.get('never_subscribed', 0),
    }

    return render(request, 'i_SO_Prospects.html', {
        'page_obj': page_obj,
        'total': total,
        'search': search,
        'status_filter': status,
        'date_from': date_from,
        'date_to': date_to,
        'page_size': page_size,
        'stats': stats,
    })


def so_prospects_action(request):
    r = _auth(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOProspect
    data    = json.loads(request.body)
    action  = data.get('action')
    user_id = get_user_id(request)

    if action == 'add':
        email = (data.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return JsonResponse({'status': 'error', 'message': 'Valid email required.'})
        p, created = SOProspect.objects.update_or_create(
            user_id=user_id, email=email,
            defaults={
                'first_name': (data.get('first_name') or '').strip(),
                'last_name':  (data.get('last_name')  or '').strip(),
                'company':    (data.get('company')    or '').strip(),
                'phone':      (data.get('phone')      or '').strip(),
                'deleted_at': None,
            },
        )
        return JsonResponse({'status': 'ok', 'id': p.id, 'created': created})

    if action == 'update':
        pid = data.get('id')
        try:
            p = SOProspect.objects.get(id=pid, user_id=user_id, deleted_at__isnull=True)
        except SOProspect.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Not found.'})
        for field in ('first_name', 'last_name', 'company', 'phone'):
            if field in data:
                setattr(p, field, (data[field] or '').strip())
        p.save()
        return JsonResponse({'status': 'ok'})

    if action == 'delete':
        pid = data.get('id')
        SOProspect.objects.filter(id=pid, user_id=user_id).update(deleted_at=now())
        return JsonResponse({'status': 'ok'})

    if action == 'bulk_delete':
        ids = data.get('ids', [])
        SOProspect.objects.filter(id__in=ids, user_id=user_id).update(deleted_at=now())
        return JsonResponse({'status': 'ok'})

    if action == 'search':
        from django.db.models import Q
        q     = (data.get('q') or '').strip()
        limit = int(data.get('limit') or 30)
        qs    = SOProspect.objects.filter(user_id=user_id, deleted_at__isnull=True)
        if q:
            qs = qs.filter(
                Q(email__icontains=q) | Q(first_name__icontains=q) |
                Q(last_name__icontains=q) | Q(company__icontains=q)
            )
        results = [
            {'id': p.id, 'email': p.email,
             'name': f'{p.first_name} {p.last_name}'.strip()}
            for p in qs[:limit]
        ]
        return JsonResponse({'status': 'ok', 'results': results})

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})


def so_prospects_import(request):
    r = _auth(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOProspect
    user_id = get_user_id(request)
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'status': 'error', 'message': 'No file uploaded.'})

    text     = f.read().decode('utf-8-sig', errors='replace')
    reader   = csv.DictReader(io.StringIO(text))
    created = updated = skipped = 0

    for row in reader:
        email = (row.get('email') or row.get('Email') or '').strip().lower()
        if not email or '@' not in email:
            skipped += 1
            continue
        _, was_created = SOProspect.objects.update_or_create(
            user_id=user_id, email=email,
            defaults={
                'first_name': (row.get('first_name') or row.get('First Name') or '').strip(),
                'last_name':  (row.get('last_name')  or row.get('Last Name')  or '').strip(),
                'company':    (row.get('company')    or row.get('Company')    or '').strip(),
                'phone':      (row.get('phone')      or row.get('Phone')      or '').strip(),
                'deleted_at': None,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return JsonResponse({'status': 'ok', 'created': created, 'updated': updated, 'skipped': skipped})
