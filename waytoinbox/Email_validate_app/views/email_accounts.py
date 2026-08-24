import json

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.urls import reverse
from django.utils.timezone import now
from django.core import signing

from Email_validate_app.utils import get_user_id


def email_accounts(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))
    from Email_validate_app.models import EmailAccount
    user_id  = get_user_id(request)
    accounts = EmailAccount.objects.filter(user_id=user_id, deleted_at__isnull=True)
    return render(request, 'i_Email_Accounts.html', {'accounts': accounts})


def add_email_account(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))
    return render(request, 'i_Add_Email_Account.html')


def email_accounts_action(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import EmailAccount

    data    = json.loads(request.body)
    action  = data.get('action')
    user_id = get_user_id(request)

    if action == 'add':
        email    = (data.get('email') or '').strip().lower()
        provider = (data.get('provider') or 'google').strip()
        first    = (data.get('first_name') or '').strip()
        last     = (data.get('last_name')  or '').strip()
        password = (data.get('password') or '').replace(' ', '')

        if not email or '@' not in email:
            return JsonResponse({'status': 'error', 'message': 'A valid email address is required.'})
        if not password:
            return JsonResponse({'status': 'error', 'message': 'App password is required.'})

        host = 'smtp.office365.com' if provider == 'microsoft' else 'smtp.gmail.com'
        port = 587
        enc_pwd = signing.dumps(password, salt='ea-pwd')

        acc = EmailAccount.objects.create(
            user_id=user_id,
            provider=provider,
            first_name=first,
            last_name=last,
            email=email,
            smtp_host=host,
            smtp_port=port,
            username=email,
            password=enc_pwd,
        )
        return JsonResponse({'status': 'ok', 'id': acc.id})

    if action == 'test':
        import smtplib
        import ssl

        acc_id = data.get('id')
        try:
            acc = EmailAccount.objects.get(id=acc_id, user_id=user_id, deleted_at__isnull=True)
        except EmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Account not found.'})

        try:
            plain_pwd = signing.loads(acc.password, salt='ea-pwd').replace(' ', '')
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Could not decrypt password.'})

        error_msg = None
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=12) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(acc.username, plain_pwd)
            new_status = 'connected'
        except smtplib.SMTPAuthenticationError:
            new_status = 'failed'
            error_msg  = (
                'Authentication failed. Make sure: '
                '(1) 2-Step Verification is enabled on your Google account, '
                '(2) you are using an App Password (not your regular password), '
                '(3) the App Password was copied without spaces.'
            )
        except smtplib.SMTPConnectError:
            new_status = 'failed'
            error_msg  = 'Could not connect to SMTP server. Check host and port.'
        except smtplib.SMTPException as e:
            new_status = 'failed'
            error_msg  = f'SMTP error: {e}'
        except Exception as e:
            new_status = 'failed'
            error_msg  = f'Connection error: {e}'

        acc.status = new_status
        acc.save(update_fields=['status', 'updated_at'])

        return JsonResponse({
            'status':     'ok',
            'result':     new_status,
            'error_msg':  error_msg,
        })

    if action == 'update_password':
        acc_id   = data.get('id')
        password = (data.get('password') or '').replace(' ', '')
        if not password:
            return JsonResponse({'status': 'error', 'message': 'Password cannot be empty.'})
        try:
            acc = EmailAccount.objects.get(id=acc_id, user_id=user_id, deleted_at__isnull=True)
        except EmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Account not found.'})
        acc.password = signing.dumps(password, salt='ea-pwd')
        acc.status   = 'unchecked'
        acc.save(update_fields=['password', 'status', 'updated_at'])
        return JsonResponse({'status': 'ok'})

    if action == 'delete':
        acc_id = data.get('id')
        EmailAccount.objects.filter(id=acc_id, user_id=user_id).update(deleted_at=now())
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})
