import json
import smtplib
import ssl

from django.core import signing
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.timezone import now

from Email_validate_app.utils import get_user_id


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def so_email_accounts(request):
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOEmailAccount, SOEmailAccountDailyUsage
    user_id  = get_user_id(request)
    accounts = list(SOEmailAccount.objects.filter(
        user_id=user_id, deleted_at__isnull=True,
    ).select_related('warmup'))

    # Today's sent count against each account's daily_limit (e.g. "35/50") —
    # same UTC-day counter services/so_drip.py reserves against before a send,
    # so this reflects the exact quota the drip dispatcher is enforcing.
    today = now().date()
    usage_map = dict(
        SOEmailAccountDailyUsage.objects.filter(
            account_id__in=[a.id for a in accounts], date=today,
        ).values_list('account_id', 'sent_count')
    )
    for acc in accounts:
        acc.today_sent = usage_map.get(acc.id, 0)
        pct = round(acc.today_sent / acc.daily_limit * 100) if acc.daily_limit else 0
        acc.limit_pct = min(pct, 100)
        if pct >= 90:
            acc.limit_level = 'critical'
        elif pct >= 70:
            acc.limit_level = 'warn'
        else:
            acc.limit_level = 'healthy'

    return render(request, 'i_SO_Email_Accounts.html', {'accounts': accounts})


def so_add_email_account(request):
    r = _auth(request)
    if r:
        return r
    return render(request, 'i_SO_Add_Email_Account.html')


def so_email_account_action(request):
    r = _auth(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOEmailAccount
    data    = json.loads(request.body)
    action  = data.get('action')
    user_id = get_user_id(request)

    # ── Add ──────────────────────────────────────────────────────────────────
    if action == 'add':
        email    = (data.get('email') or '').strip().lower()
        provider = (data.get('provider') or 'google').strip()
        display  = (data.get('display_name') or '').strip()
        password = (data.get('password') or '').replace(' ', '')

        if not email or '@' not in email:
            return JsonResponse({'status': 'error', 'message': 'A valid email address is required.'})
        if not password:
            return JsonResponse({'status': 'error', 'message': 'App password is required.'})

        if provider == 'microsoft':
            smtp_host, imap_host = 'smtp.office365.com', 'outlook.office365.com'
        else:
            smtp_host, imap_host = 'smtp.gmail.com', 'imap.gmail.com'

        enc_pwd = signing.dumps(password, salt='so-ea-pwd')
        acc = SOEmailAccount.objects.create(
            user_id=user_id, provider=provider, display_name=display,
            email=email, smtp_host=smtp_host, smtp_port=587,
            imap_host=imap_host, imap_port=993, imap_ssl=True,
            username=email, password=enc_pwd,
        )
        return JsonResponse({'status': 'ok', 'id': acc.id})

    # ── Test (SMTP) ───────────────────────────────────────────────────────────
    if action == 'test':
        acc_id = data.get('id')
        try:
            acc = SOEmailAccount.objects.get(id=acc_id, user_id=user_id, deleted_at__isnull=True)
        except SOEmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Account not found.'})
        try:
            plain_pwd = signing.loads(acc.password, salt='so-ea-pwd').replace(' ', '')
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Could not decrypt password.'})

        error_msg = None
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=12) as server:
                server.ehlo(); server.starttls(context=ctx); server.ehlo()
                server.login(acc.username, plain_pwd)
            new_status = 'connected'
        except smtplib.SMTPAuthenticationError:
            new_status = 'failed'
            error_msg  = ('Authentication failed. Make sure: (1) 2-Step Verification is enabled, '
                          '(2) you are using an App Password, (3) the App Password has no spaces.')
        except Exception as e:
            new_status = 'failed'
            error_msg  = f'Connection error: {e}'

        acc.status = new_status
        acc.save(update_fields=['status', 'updated_at'])
        return JsonResponse({'status': 'ok', 'result': new_status, 'error_msg': error_msg})

    # ── Update Password ───────────────────────────────────────────────────────
    if action == 'update_password':
        acc_id   = data.get('id')
        password = (data.get('password') or '').replace(' ', '')
        if not password:
            return JsonResponse({'status': 'error', 'message': 'Password cannot be empty.'})
        try:
            acc = SOEmailAccount.objects.get(id=acc_id, user_id=user_id, deleted_at__isnull=True)
        except SOEmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Account not found.'})
        acc.password = signing.dumps(password, salt='so-ea-pwd')
        acc.status   = 'unchecked'
        acc.save(update_fields=['password', 'status', 'updated_at'])
        return JsonResponse({'status': 'ok'})

    # ── Delete ────────────────────────────────────────────────────────────────
    if action == 'delete':
        acc_id = data.get('id')
        SOEmailAccount.objects.filter(id=acc_id, user_id=user_id).update(deleted_at=now())
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})
