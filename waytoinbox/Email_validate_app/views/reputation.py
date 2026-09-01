import re
import logging
from datetime import datetime
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Max

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone

from Email_validate_app.models import (
    CurrentCredits, SubsPayment, Reputation, ReputationResults, ServiceCredit,
)
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.credit_manager import (
    get_ac_current_credit, get_effective_balance, deduct_service_credits,
    InsufficientCredits,
)

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$")


def _get_ac_subscription_context(user_id):
    """Return (ac_current, plan_total, ac_used, plan_name, plan_valid_till)."""
    ac_current = ac_used = 0
    try:
        cobj = CurrentCredits.objects.filter(user_id=user_id).first()
        if cobj:
            ac_current = cobj.ac_current_credits or 0
            ac_used    = cobj.ac_used_credits    or 0
    except Exception:
        pass

    plan_total = 0
    plan_name = plan_valid_till = None
    try:
        sub = SubsPayment.objects.filter(
            user_id=user_id, plan_status='Active'
        ).order_by('-payment_time').first()
        if sub:
            plan_name       = sub.subs_plan
            plan_valid_till = sub.valid_time
            plan_total      = int(sub.ac_credits) if sub.ac_credits else 0
    except Exception:
        pass

    if not plan_total:
        try:
            cobj = CurrentCredits.objects.filter(user_id=user_id).first()
            plan_total = (cobj.ac_total_credits or 0) if cobj else 0
        except Exception:
            plan_total = 0

    return ac_current, plan_total, ac_used, plan_name, plan_valid_till


def Reputation_Analysis(request):
    # Auth guard
    if not request.session.get('logged_in'):
        return redirect('login')

    user_id = get_user_id(request)

    if request.method != 'POST':
        ac_current, plan_total, ac_used, plan_name, plan_valid_till = (
            _get_ac_subscription_context(user_id) if user_id else (0, 0, 0, None, None)
        )
        from Email_validate_app.services.filter_status import REPUTATION_STATUSES
        return render(request, 'i_Reputation_Analysis.html', {
            'credits':            ac_current,
            'ac_current_credits': ac_current,
            'ac_total_credits':   plan_total,
            'ac_used_credits':    ac_used,
            'plan_name':          plan_name,
            'plan_valid_till':    plan_valid_till,
            'pf_statuses':        REPUTATION_STATUSES,
        })

    # Get & normalize domain
    domain = request.POST.get('domain_input', '').strip().lower()

    if not domain:
        return JsonResponse({'status': 'error', 'message': 'Domain is required.'})

    # Strip protocol (http:// / https://)
    parsed = urlparse(domain)
    if parsed.netloc:
        domain = parsed.netloc
    elif parsed.path and not parsed.scheme:
        domain = parsed.path

    # Remove any trailing path/query that crept in
    domain = domain.split('/')[0].split('?')[0]

    # Validate format
    if not _DOMAIN_RE.match(domain):
        return JsonResponse({'status': 'error', 'message': 'Enter a valid domain.'})

    # Session user
    if not user_id:
        return JsonResponse({'status': 'error', 'message': 'User session not found.'})

    # Credit check. Phase 6 commit 4: the balance is the reputation service
    # wallet plus the legacy AC pool behind it, rather than the raw AC column.
    # That legacy half is still ONE pool shared with Header Analyzer and the
    # two blocklist monitors — it is spent in place, never copied here.
    if get_effective_balance(user_id, 'reputation') <= 0:
        return JsonResponse({
            'status':  'error',
            'message': 'No Analysis Credits left. Purchase more credits to continue.',
            'no_credits': True,
            'ac_current_credits': 0,
        })

    # Duplicate check (ignore soft-deleted records)
    if Reputation.objects.filter(user_id=user_id, domain=domain, deleted_at__isnull=True).exists():
        return JsonResponse({
            'status': 'warning',
            'message': f'Domain "{domain}" already exists.',
        })

    # Postmaster lookup happens BEFORE anything is charged, and deliberately
    # outside the transaction below: it is a network call, and holding row
    # locks across it would be a long-lived lock for no benefit. A failure
    # here now costs the user nothing, where previously the credit had already
    # been taken.
    from Email_validate_app.services.postmaster import fetch_domain_traffic_stats
    stats = fetch_domain_traffic_stats(domain)

    # Determine verification status
    rep_status = 'verified' if stats else 'unverified'

    # The record and the charge share one transaction, so a failed insert can
    # never leave the credit spent and a failed charge can never leave an
    # unpaid record. No manual refund is involved.
    try:
        with transaction.atomic():
            # Same lock, and the same lock order (ServiceCredit first), that
            # deduct_service_credits() uses a moment later — so concurrent
            # requests for one domain serialise here and the duplicate re-check
            # below is reliable.
            ServiceCredit.objects.select_for_update().filter(
                user_id=user_id, service='reputation',
            ).first()

            if Reputation.objects.filter(
                user_id=user_id, domain=domain, deleted_at__isnull=True,
            ).exists():
                return JsonResponse({
                    'status': 'warning',
                    'message': f'Domain "{domain}" already exists.',
                })

            # Persist Reputation row
            rep = Reputation.objects.create(
                user_id=user_id,
                domain=domain,
                status=rep_status,
            )

            deduct_service_credits(
                user_id, 'reputation', 1,
                ref_type='reputation', ref_id=domain,
                description='Reputation Analysis',
            )
    except InsufficientCredits:
        return JsonResponse({
            'status': 'error',
            'message': 'No Analysis Credits left. Purchase more credits to continue.',
            'no_credits': True,
        })

    new_ac_current = get_ac_current_credit(user_id)
    if rep_status == 'unverified':
        try:
            from Email_validate_app.utils import create_notification
            create_notification(user_id, 'reputation',
                f'Reputation check for "{domain}" — domain is unverified',
                url='/Reputation_Analysis/')
        except Exception:
            pass
        try:
            from Email_validate_app.models import UserTable as _UT
            _u = _UT.objects.filter(pk=user_id).only('user_name', 'user_email', 'notify_reputation').first()
            if _u and getattr(_u, 'notify_reputation', True):
                from Email_validate_app.services.mailer import send_reputation_unverified_email
                send_reputation_unverified_email(_u.user_name, _u.user_email, domain)
        except Exception:
            pass
    try:
        from Email_validate_app.services.mailer import send_admin_reputation_added_notification
        send_admin_reputation_added_notification(
            request.session.get('logged_in', ''), domain, rep_status
        )
    except Exception:
        pass

    # Persist ReputationResults rows (skip existing date entries)
    if stats:
        for item in stats:
            try:
                date_obj = datetime.strptime(item['date'], '%Y%m%d').date()
            except (ValueError, KeyError):
                logger.warning(
                    "Skipping Postmaster stat with unparseable date: %s", item.get('date')
                )
                continue

            if ReputationResults.objects.filter(domain=domain, date=date_obj).exists():
                continue

            ReputationResults.objects.create(
                domain=domain,
                date=date_obj,
                spam_rate=item.get('spam_rate'),
                domain_reputation=item.get('domain_reputation'),
                ip_reputation=item.get('ip_reputation', []),
                delivery_errors=item.get('delivery_errors', []),
            )

    last_checked_date = ReputationResults.objects.filter(domain=domain).aggregate(
        last_date=Max('date')
    )['last_date']

    return JsonResponse({
        'status':               'ok',
        'rep_id':               rep.id,
        'domain':               rep.domain,
        'rep_status':           rep.status,
        'date':                 rep.created_at.strftime('%Y-%m-%d'),
        'last_checked':         last_checked_date.strftime('%Y-%m-%d') if last_checked_date else None,
        'ac_current_credits':   new_ac_current,
    })


def get_reputation_data(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'data': []})

    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'data': []})

    try:
        records = list(Reputation.objects.filter(user_id=user_id, is_hidden=False).order_by('-created_at'))
    except Exception:
        records = list(Reputation.objects.filter(user_id=user_id).order_by('-created_at'))

    domains = [r.domain for r in records]
    last_checked_map = {}
    if domains:
        last_checked_map = dict(
            ReputationResults.objects.filter(domain__in=domains)
            .values('domain')
            .annotate(last_date=Max('date'))
            .values_list('domain', 'last_date')
        )

    data = [
        {
            'id':           r.id,
            'domain':       r.domain,
            'status':       r.status,
            'date':         r.created_at.strftime('%Y-%m-%d'),
            'last_checked': last_checked_map[r.domain].strftime('%Y-%m-%d') if last_checked_map.get(r.domain) else None,
        }
        for r in records
    ]

    return JsonResponse({'data': data})


@require_POST
def hide_reputation_row(request):
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'status': 'error', 'message': 'Login required.'}, status=401)

    record_id = request.POST.get('record_id')
    if not record_id:
        return JsonResponse({'status': 'error', 'message': 'Invalid parameters.'}, status=400)

    updated = Reputation.objects.filter(
        id=record_id,
        user_id=user_id,
        is_hidden=False,
    ).update(is_hidden=True, deleted_at=timezone.now())

    if updated:
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'status': 'error', 'message': 'Record not found.'}, status=404)


def reputation_detail(request, rep_id):
    if not request.session.get('logged_in'):
        return redirect('login')

    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    rep = get_object_or_404(Reputation, id=rep_id, user_id=user_id, is_hidden=False)
    results = ReputationResults.objects.filter(domain=rep.domain).order_by('-date')

    return render(request, 'i_Reputation_Detail.html', {
        'rep': rep,
        'results': results,
    })
