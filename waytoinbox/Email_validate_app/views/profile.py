import json

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone

from Email_validate_app.models import (
    UserTable, CurrentCredits, EmailValidate, ListFiles,
    BlocklistMonitor, DomainBlocklist, EmailHeader, APIKey,
    SubsPayment, Payment, Reputation, Campaign, DMARCAnalysis,
)
from Email_validate_app.utils import get_user_id

from .billing import get_current_credit, get_ac_current_credit


def profile(request):
    if not request.session.get('logged_in'):
        return redirect('login')
    import pytz
    user_id = get_user_id(request)
    current_credits = 0
    ip_credits      = 0
    user            = None
    api_keys        = []
    payments        = []
    active_plan     = None
    active_sub      = None
    credit_row      = None
    login_logs      = []
    pi_company = pi_role = pi_timezone = pi_website = ''

    if user_id:
        current_credits = get_current_credit(user_id)
        ip_credits      = get_ac_current_credit(user_id)

        # Fetch user — use only() to avoid crashing if new columns not migrated yet
        try:
            user = UserTable.objects.filter(pk=user_id).first()
        except Exception:
            user = UserTable.objects.only(
                'id', 'user_name', 'user_email', 'is_verified', 'created_date'
            ).filter(pk=user_id).first()

        api_keys         = APIKey.objects.filter(user_id=user_id).order_by('-created_at')
        payments         = SubsPayment.objects.filter(user_id=user_id, is_hidden=False).order_by('-payment_time')[:1]
        onetime_payments = Payment.objects.filter(user_id=user_id, is_hidden=False).order_by('-payment_time')[:1]

        try:
            sub = SubsPayment.objects.filter(user_id=user_id, plan_status="Active").latest('payment_time')
            active_plan = sub.subs_plan
            active_sub  = sub
        except SubsPayment.DoesNotExist:
            active_plan = None
            active_sub  = None

        # Determine feature group from contact count stored in cc_credits
        plan_group = 1
        if active_sub and active_sub.cc_credits:
            try:
                cc = int(active_sub.cc_credits)
                if cc <= 5000:
                    plan_group = 1
                elif cc <= 25000:
                    plan_group = 2
                else:
                    plan_group = 3
            except (ValueError, TypeError):
                plan_group = 1

        # New profile fields — safe access in case migration not yet run
        pi_company  = getattr(user, 'company',  None) or ''
        pi_role     = getattr(user, 'role',     None) or ''
        pi_timezone = getattr(user, 'timezone', None) or ''
        pi_website  = getattr(user, 'website',  None) or ''

        try:
            credit_row = CurrentCredits.objects.get(user_id=user_id)
        except CurrentCredits.DoesNotExist:
            credit_row = None

        from Email_validate_app.models import LoginActivity
        login_logs = LoginActivity.objects.filter(user_id=user_id, status='success')[:5]

    # Split stored name into first / last
    name_parts = (user.user_name if user else '').split(' ', 1)
    first_name = name_parts[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else ''

    return render(request, "i_profile.html", {
        'credits':     current_credits,
        'ip_credits':  ip_credits,
        'username':    user.user_name  if user else '',
        'user_email':  user.user_email if user else '',
        'first_name':  first_name,
        'last_name':   last_name,
        'api_keys':    api_keys,
        'payments':    payments,
        'joined_date': user.created_date if user else None,
        'user':        user,
        'active_plan': active_plan,
        'timezones':   pytz.all_timezones,
        'pi_company':  pi_company,
        'pi_role':     pi_role,
        'pi_timezone': pi_timezone,
        'pi_website':  pi_website,
        'notify_job_complete':  getattr(user, 'notify_job_complete',  True),
        'notify_blocklist':     getattr(user, 'notify_blocklist',     True),
        'notify_payment':       getattr(user, 'notify_payment',       True),
        'notify_expiry':        getattr(user, 'notify_expiry',        True),
        'notify_campaign':      getattr(user, 'notify_campaign',      True),
        'notify_reputation':    getattr(user, 'notify_reputation',    True),
        'notify_sender_verify': getattr(user, 'notify_sender_verify', True),
        'active_sub':           active_sub,
        'plan_group':           plan_group,
        'onetime_payments':     onetime_payments,
        'credit_row':           credit_row,
        'login_logs':           login_logs,
        'password_changed_at':  getattr(user, 'password_changed_at', None),
    })


def profile_activity_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False}, status=401)
    user_id = get_user_id(request)
    activities = []
    if user_id:
        ev = EmailValidate.objects.filter(user_id=user_id).only(
            'email', 'mx_found', 'insert_date'
        ).order_by('-insert_date').first()
        if ev:
            r = (ev.mx_found or '').lower()
            activities.append({
                'type':   'email_verify',
                'msg':    'Email Verify',
                'detail': ev.email or '—',
                'result': ev.mx_found or 'Unknown',
                'time':   ev.insert_date.isoformat() if ev.insert_date else None,
                'icon':   'green' if r == 'valid' else ('red' if r == 'invalid' else 'amber'),
            })

        job = ListFiles.objects.filter(user_id=user_id).only(
            'table_name', 'job_status', 'insert_date'
        ).order_by('-insert_date').first()
        if job:
            status = job.job_status or 'Unknown'
            activities.append({
                'type':   'bulk_verify',
                'msg':    'Bulk Verify',
                'detail': job.table_name or '—',
                'result': status,
                'time':   job.insert_date.isoformat() if job.insert_date else None,
                'icon':   'green' if status.lower() == 'complete' else 'blue',
            })

        c = Campaign.objects.filter(
            user_id=user_id, deleted_at__isnull=True, sent_at__isnull=False
        ).only('campaign_name', 'status', 'sent_at').order_by('-sent_at').first()
        if c:
            activities.append({
                'type':   'campaign',
                'msg':    'Campaign',
                'detail': c.campaign_name or '—',
                'result': c.status.capitalize() if c.status else '—',
                'time':   c.sent_at.isoformat(),
                'icon':   'green' if c.status == 'sent' else ('red' if c.status == 'failed' else 'amber'),
            })

        rep = Reputation.objects.filter(
            user_id=user_id, is_hidden=False, deleted_at__isnull=True
        ).only('domain', 'status', 'created_at').order_by('-created_at').first()
        if rep:
            activities.append({
                'type':   'reputation',
                'msg':    'Reputation Analysis',
                'detail': rep.domain or '—',
                'result': rep.status.capitalize() if rep.status else 'Analyzed',
                'time':   rep.created_at.isoformat() if rep.created_at else None,
                'icon':   'green' if rep.status and rep.status.lower() == 'verified' else 'amber',
            })

        ha = EmailHeader.objects.filter(user_id=user_id).only(
            'subject', 'from_email', 'created_at'
        ).order_by('-created_at').first()
        if ha:
            activities.append({
                'type':   'header_analysis',
                'msg':    'Header Analysis',
                'detail': ha.subject or ha.from_email or '—',
                'result': 'Analyzed',
                'time':   ha.created_at.isoformat() if ha.created_at else None,
                'icon':   'amber',
            })

        dmarc = DMARCAnalysis.objects.filter(
            user=user_id, is_hidden=False
        ).only('domain', 'spf_status', 'dmarc_status', 'dkim_status', 'created_at').order_by('-created_at').first()
        if dmarc:
            passed = sum([
                dmarc.spf_status == 'pass',
                dmarc.dmarc_status == 'pass',
                dmarc.dkim_status == 'pass',
            ])
            activities.append({
                'type':   'dmarc',
                'msg':    'DMARC Analysis',
                'detail': dmarc.domain or '—',
                'result': f'{passed}/3 passed',
                'time':   dmarc.created_at.isoformat() if dmarc.created_at else None,
                'icon':   'green' if passed == 3 else ('amber' if passed > 0 else 'red'),
            })

        b = BlocklistMonitor.objects.filter(
            user_id=user_id, is_hidden=False
        ).only('ips', 'listed_count', 'created_date').order_by('-created_date').first()
        if b:
            listed = b.listed_count and b.listed_count not in ('', '0')
            activities.append({
                'type':   'ip_blocklist',
                'msg':    'IP Blocklist',
                'detail': b.ips or '—',
                'result': 'Listed' if listed else 'Clean',
                'time':   b.created_date.isoformat() if b.created_date else None,
                'icon':   'red' if listed else 'green',
            })

        d = DomainBlocklist.objects.filter(
            user_id=user_id, is_hidden=False
        ).only('domain', 'listed_count', 'created_date').order_by('-created_date').first()
        if d:
            listed = d.listed_count and d.listed_count not in ('', '0')
            activities.append({
                'type':   'domain_blocklist',
                'msg':    'Domain Blocklist',
                'detail': d.domain or '—',
                'result': 'Listed' if listed else 'Clean',
                'time':   d.created_date.isoformat() if d.created_date else None,
                'icon':   'red' if listed else 'green',
            })

        activities.sort(key=lambda x: x['time'] or '', reverse=True)

    return JsonResponse({'ok': True, 'activities': activities})


def profile_update_ajax(request):
    import urllib.request as urllib_req
    import urllib.error
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False, 'error': 'Not logged in'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Method not allowed'}, status=405)
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'User not found'}, status=404)
    data       = json.loads(request.body)
    first_name = data.get('first_name', '').strip()
    last_name  = data.get('last_name', '').strip()
    company    = data.get('company', '').strip()
    role       = data.get('role', '').strip()
    timezone_val = data.get('timezone', '').strip()
    website    = data.get('website', '').strip()
    full_name  = (first_name + ' ' + last_name).strip()

    # Validate required fields
    missing = []
    if not first_name: missing.append('First name')
    if not last_name:  missing.append('Last name')
    if not company:    missing.append('Company')
    if not role:       missing.append('Role')
    if not timezone_val:   missing.append('Timezone')
    if not website:    missing.append('Website')
    if missing:
        return JsonResponse({'ok': False, 'error': f"Please fill in: {', '.join(missing)}", 'missing': missing})

    # Check website exists in the real world
    url = website if website.startswith(('http://', 'https://')) else 'https://' + website
    try:
        req = urllib_req.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        urllib_req.urlopen(req, timeout=6)
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            return JsonResponse({'ok': False, 'error': f'Website returned server error ({e.code}). Please check the URL.', 'field': 'website'})
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Website does not exist or is unreachable. Please check the URL.', 'field': 'website'})

    update_fields = {
        'user_name': full_name,
        'company':   company,
        'role':      role,
        'timezone':  timezone_val,
        'website':   url,
    }
    UserTable.objects.filter(pk=user_id).update(**update_fields)
    return JsonResponse({'ok': True, 'name': full_name})


def delete_account_request(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False, 'error': 'Not authenticated'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'User not found'}, status=404)
    try:
        import json as _json
        data        = _json.loads(request.body)
        reason      = data.get('reason', '').strip()
        extra_note  = data.get('extra_note', '').strip()
        if not reason:
            return JsonResponse({'ok': False, 'error': 'Please select a reason.'})
        user = UserTable.objects.filter(pk=user_id).first()
        if not user:
            return JsonResponse({'ok': False, 'error': 'User not found'})
        try:
            active_sub = SubsPayment.objects.filter(user_id=user_id, plan_status='Active').latest('payment_time')
            active_plan = active_sub.subs_plan
        except SubsPayment.DoesNotExist:
            active_plan = None
        joined = user.created_date.strftime('%d %b %Y') if user.created_date else '—'
        from Email_validate_app.services.mailer import send_delete_request_email
        send_delete_request_email(
            user_id=user_id,
            user_name=user.user_name,
            user_email=user.user_email,
            joined=joined,
            active_plan=active_plan,
            reason=reason,
            extra_note=extra_note,
        )
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


def change_password_ajax(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False, 'error': 'Not authenticated'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    import json
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    current_pw  = body.get('current_password', '').strip()
    new_pw      = body.get('new_password', '').strip()
    confirm_pw  = body.get('confirm_password', '').strip()

    if not current_pw or not new_pw or not confirm_pw:
        return JsonResponse({'ok': False, 'error': 'All fields are required.'})
    if len(new_pw) < 8:
        return JsonResponse({'ok': False, 'error': 'New password must be at least 8 characters.'})
    if new_pw != confirm_pw:
        return JsonResponse({'ok': False, 'error': 'New passwords do not match.'})

    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'ok': False, 'error': 'User not found.'}, status=404)

    user = UserTable.objects.filter(pk=user_id).first()
    if not user:
        return JsonResponse({'ok': False, 'error': 'User not found.'}, status=404)

    if not user.check_password(current_pw):
        return JsonResponse({'ok': False, 'error': 'Current password is incorrect.'})
    if new_pw == current_pw:
        return JsonResponse({'ok': False, 'error': 'New password must be different from your current password.'})

    user.set_password(new_pw)
    user.password_changed_at = timezone.now()
    user.save()
    # INF-06: rotate session key after password change to prevent stolen-session reuse
    request.session.cycle_key()
    return JsonResponse({'ok': True})


def notifications_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False}, status=401)
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'ok': False}, status=404)
    from Email_validate_app.models import UserNotification
    preview = request.GET.get('preview') == '1'
    limit = 5 if preview else 30
    notifs = UserNotification.objects.filter(user_id=user_id).order_by('-created_at')[:limit]
    unread_count = UserNotification.objects.filter(user_id=user_id, is_read=False).count()
    data = [
        {
            'id':      n.id,
            'type':    n.type,
            'message': n.message,
            'url':     n.url,
            'is_read': n.is_read,
            'time':    n.created_at.isoformat(),
        }
        for n in notifs
    ]
    if not preview:
        UserNotification.objects.filter(user_id=user_id, is_read=False).update(is_read=True)
    return JsonResponse({'ok': True, 'notifications': data, 'unread_count': unread_count})


def notifications_count_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'count': 0})
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'count': 0})
    from Email_validate_app.models import UserNotification
    count = UserNotification.objects.filter(user_id=user_id, is_read=False).count()
    return JsonResponse({'count': count})


def notification_update_ajax(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'ok': False}, status=401)
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    import json
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'ok': False}, status=404)

    allowed = {
        'notify_job_complete', 'notify_blocklist', 'notify_payment', 'notify_expiry',
        'notify_campaign', 'notify_reputation', 'notify_sender_verify',
    }
    update_fields = {k: bool(v) for k, v in body.items() if k in allowed}
    if update_fields:
        UserTable.objects.filter(pk=user_id).update(**update_fields)
    return JsonResponse({'ok': True})
