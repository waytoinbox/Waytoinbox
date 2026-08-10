import logging

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.urls import reverse
from django.conf import settings

from Email_validate_app.utils import get_user_id
from Email_validate_app.services.sender_domain import _sync_domain_status

logger = logging.getLogger(__name__)


def sender_verify(request):
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    from Email_validate_app.models import SenderDomain, SenderEmailToken

    user_id = get_user_id(request)
    ctx = {'domains': [], 'emails': [], 'error': None}

    db_domains = SenderDomain.objects.filter(user_id=user_id, is_hidden=False).order_by('-added_at')
    for d in db_domains:
        # Sync both providers whenever either is still pending
        if d.ses_status != 'verified' or d.mailgun_status != 'verified':
            _sync_domain_status(d)

        ctx['domains'].append({
            'identity':          d.domain,
            'verified':          d.status == 'verified',
            'dkim_status':       d.dkim_status,
            'dkim_tokens':       d.dkim_tokens,
            'ses_status':        d.ses_status,
            'ses_dkim_tokens':   d.ses_dkim_tokens,
            'ses_verified_at':   d.ses_verified_at,
            'mailgun_status':    d.mailgun_status,
            'mailgun_dkim_tokens': d.mailgun_dkim_tokens,
            'mailgun_verified_at': d.mailgun_verified_at,
            'added_at':          d.added_at,
            'verified_at':       d.verified_at,
        })

    from Email_validate_app.models import SenderDomain as _SD
    _domain_status_map = {
        d.domain: d.status
        for d in _SD.objects.filter(user_id=user_id, is_hidden=False)
    }
    db_emails = SenderEmailToken.objects.filter(user_id=user_id, is_hidden=False).order_by('-created_at')
    for e in db_emails:
        domain_part = e.email.split('@')[-1]
        ctx['emails'].append({
            'email':         e.email,
            'domain':        domain_part,
            'domain_status': _domain_status_map.get(domain_part, 'not_found'),
            'created_at':    e.created_at,
        })

    return render(request, 'i_Sender_Verify.html', ctx)


def sender_verify_action(request):
    """AJAX: add/verify/delete a sender identity."""
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    import json as _json
    try:
        _body = _json.loads(request.body)
    except (ValueError, TypeError):
        _body = {}
    identity = _body.get('identity', '').strip().lower()
    action   = _body.get('action', '').strip()

    if not identity:
        return JsonResponse({'status': 'error', 'message': 'Identity is required.'}, status=400)

    user_id = get_user_id(request)

    try:
        if action == 'check_email':
            import secrets
            from Email_validate_app.models import SenderEmailToken, SenderDomain

            if not identity or '@' not in identity:
                return JsonResponse({'status': 'error', 'message': 'Invalid email address.'}, status=400)

            domain_part = identity.split('@')[-1]
            domain_row = SenderDomain.objects.filter(domain=domain_part, user_id=user_id, is_hidden=False).first()
            domain_status = domain_row.status if domain_row else 'not_found'

            # Save or restore the email record (no token used for verification)
            existing = SenderEmailToken.objects.filter(email=identity, user_id=user_id, is_hidden=False).first()
            if existing:
                return JsonResponse({
                    'status':        'ok',
                    'domain':        domain_part,
                    'domain_status': domain_status,
                    'already_added': True,
                })

            hidden = SenderEmailToken.objects.filter(email=identity, user_id=user_id, is_hidden=True).first()
            if hidden:
                hidden.is_hidden = False
                hidden.deleted_at = None
                hidden.confirmed = (domain_status == 'verified')
                hidden.save(update_fields=['is_hidden', 'deleted_at', 'confirmed'])
            else:
                SenderEmailToken.objects.create(
                    email=identity,
                    token=secrets.token_urlsafe(32),
                    user_id=user_id,
                    confirmed=(domain_status == 'verified'),
                )

            return JsonResponse({
                'status':        'ok',
                'domain':        domain_part,
                'domain_status': domain_status,
                'already_added': False,
            })

        elif action in ('verify_domain', 'dns_records'):
            from Email_validate_app.models import SenderDomain
            from Email_validate_app.services.providers.ses_provider import SESProvider
            from Email_validate_app.services.providers.mailgun_provider import MailgunProvider

            if action == 'verify_domain':
                if SenderDomain.objects.filter(domain=identity, user_id=user_id, is_hidden=False).exists():
                    return JsonResponse(
                        {'status': 'error', 'message': f'{identity} is already added.'},
                        status=400,
                    )

                # Register with both providers — independent of EMAIL_PROVIDER
                ses_records = []
                mg_records  = []
                ses_error   = None
                mg_error    = None

                try:
                    ses_result  = SESProvider().create_domain(identity)
                    ses_records = ses_result.get('dns_records', [])
                except Exception as exc:
                    ses_error = str(exc)
                    logger.warning('SES create_domain failed for %s: %s', identity, exc)

                try:
                    mg_result  = MailgunProvider().create_domain(identity)
                    mg_records = mg_result.get('dns_records', [])
                except Exception as exc:
                    mg_error = str(exc)
                    logger.warning('Mailgun create_domain failed for %s: %s', identity, exc)

                # Need at least one provider to return DNS records
                if not ses_records and not mg_records:
                    detail = []
                    if ses_error: detail.append(f'SES: {ses_error}')
                    if mg_error:  detail.append(f'Mailgun: {mg_error}')
                    return JsonResponse(
                        {'status': 'error', 'message': f'Failed to register {identity} with both providers. ' + ' | '.join(detail)},
                        status=500,
                    )

                # SES records are canonical fallback; Mailgun records are additive
                canonical_tokens = ses_records or mg_records

                SenderDomain.objects.create(
                    user_id=user_id,
                    domain=identity,
                    status='pending',
                    dkim_status='PENDING',
                    dkim_tokens=canonical_tokens,
                    ses_status='pending',
                    ses_dkim_tokens=ses_records,
                    mailgun_status='pending',
                    mailgun_dkim_tokens=mg_records,
                )
                try:
                    from Email_validate_app.services.mailer import send_admin_domain_added_notification
                    send_admin_domain_added_notification(
                        request.session.get('logged_in', ''), identity, 'dual'
                    )
                except Exception:
                    pass
                try:
                    from Email_validate_app.models import UserTable as _AdminUT
                    from Email_validate_app.utils import create_notification
                    _user_email = request.session.get('logged_in', 'unknown')
                    for _admin in _AdminUT.objects.filter(is_admin=True, is_active=True).only('id'):
                        create_notification(
                            _admin.id, 'sender_verify',
                            f'Domain "{identity}" added by {_user_email} — pending verification',
                            url='/Email_Campaigns/sender-verify/',
                        )
                except Exception:
                    pass

                # Build warning if one provider failed
                warning = None
                if ses_error and not ses_records:
                    warning = f'SES registration failed: {ses_error}'
                elif mg_error and not mg_records:
                    warning = f'Mailgun registration failed: {mg_error}'

                return JsonResponse({
                    'status':          'ok',
                    'ses_records':     ses_records,
                    'mailgun_records': mg_records,
                    'dkim_tokens':     canonical_tokens,
                    'warning':         warning,
                    'message':         f'Domain {identity} registered. Add all DNS records below to your DNS provider.',
                })

            else:  # dns_records
                db_row = SenderDomain.objects.filter(domain=identity, user_id=user_id, is_hidden=False).first()
                ses_records = (db_row.ses_dkim_tokens if db_row else []) or []
                mg_records  = (db_row.mailgun_dkim_tokens if db_row else []) or []
                save_fields = []

                # Fallback: fetch from providers if empty, then persist
                if not ses_records:
                    try:
                        ses_records = SESProvider().create_domain(identity).get('dns_records', [])
                        if ses_records and db_row:
                            db_row.ses_dkim_tokens = ses_records
                            save_fields.append('ses_dkim_tokens')
                    except Exception:
                        pass
                if not mg_records:
                    try:
                        mg_records = MailgunProvider().create_domain(identity).get('dns_records', [])
                        if mg_records and db_row:
                            db_row.mailgun_dkim_tokens = mg_records
                            save_fields.append('mailgun_dkim_tokens')
                    except Exception:
                        pass
                if save_fields and db_row:
                    db_row.save(update_fields=save_fields)

                return JsonResponse({
                    'status':          'ok',
                    'ses_records':     ses_records,
                    'mailgun_records': mg_records,
                    'dkim_tokens':     ses_records or mg_records,
                    'message':         f'Add the DNS records below for {identity}.',
                })

        elif action == 'refresh_domain':
            from Email_validate_app.models import SenderDomain
            db_row = SenderDomain.objects.filter(domain=identity, user_id=user_id, is_hidden=False).first()
            if not db_row:
                return JsonResponse({'status': 'error', 'message': 'Domain not found.'}, status=404)
            _sync_domain_status(db_row)
            return JsonResponse({
                'status':          'ok',
                'domain_status':   db_row.status,
                'dkim_status':     db_row.dkim_status,
                'ses_status':      db_row.ses_status,
                'mailgun_status':  db_row.mailgun_status,
            })

        elif action == 'delete':
            from Email_validate_app.models import SenderDomain, SenderEmailToken
            from django.utils import timezone
            now = timezone.now()
            domain_deleted = SenderDomain.objects.filter(domain=identity, user_id=user_id, is_hidden=False).update(
                is_hidden=True, deleted_at=now
            )
            if domain_deleted:
                SenderEmailToken.objects.filter(
                    user_id=user_id, is_hidden=False, email__iendswith='@' + identity
                ).update(is_hidden=True, deleted_at=now)
            else:
                SenderEmailToken.objects.filter(email=identity, user_id=user_id, is_hidden=False).update(
                    is_hidden=True, deleted_at=now
                )
            return JsonResponse({'status': 'ok', 'message': f'{identity} removed.'})

        else:
            return JsonResponse({'status': 'error', 'message': 'Unknown action.'}, status=400)

    except Exception as exc:
        return JsonResponse({'status': 'error', 'message': str(exc)}, status=500)


def sender_verify_confirm(request, token):
    """User lands here after clicking the link in the verification email."""
    from Email_validate_app.models import SenderEmailToken

    try:
        obj = SenderEmailToken.objects.get(token=token, confirmed=False)
    except SenderEmailToken.DoesNotExist:
        return HttpResponse('''<!doctype html><html><head><meta charset="utf-8">
<title>Invalid Link</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#f5f6fa;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);padding:48px 40px;text-align:center;max-width:420px;width:100%}
.icon{font-size:48px;margin-bottom:16px}.title{font-size:20px;font-weight:700;color:#111;margin-bottom:8px}
.sub{font-size:14px;color:#666;line-height:1.6}</style></head>
<body><div class="card">
<div class="icon">&#10060;</div>
<div class="title">Link Invalid or Expired</div>
<div class="sub">This verification link has already been used or is no longer valid.<br>Please request a new verification email from the Sender Verify page.</div>
</div></body></html>''', status=400)

    # Register the email identity with the active provider (no-op for Mailgun)
    try:
        from Email_validate_app.services.providers import get_provider
        get_provider().create_email_identity(obj.email)
    except Exception:
        pass

    obj.confirmed = True
    obj.save(update_fields=['confirmed'])

    try:
        from Email_validate_app.utils import create_notification
        create_notification(obj.user_id, 'sender_verify',
            f'Email address "{obj.email}" has been verified successfully',
            url='/Email_Campaigns/sender-verify/')
    except Exception:
        pass

    return HttpResponse('''<!doctype html><html><head><meta charset="utf-8">
<title>Email Verified</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#f5f6fa;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);padding:48px 40px;text-align:center;max-width:420px;width:100%}
.icon{font-size:48px;margin-bottom:16px}.title{font-size:20px;font-weight:700;color:#111;margin-bottom:8px}
.sub{font-size:14px;color:#666;line-height:1.6}.btn{display:inline-block;margin-top:24px;padding:10px 24px;background:#0d9488;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}
</style></head>
<body><div class="card">
<div class="icon">&#9989;</div>
<div class="title">Email Verified!</div>
<div class="sub">Your sender email address has been confirmed.<br>You can now use it in campaigns.</div>
<a class="btn" href="/sender-verify/">Go to Sender Verify</a>
</div></body></html>''')


def sender_verify_dns_page(request):
    """Dedicated page for DKIM DNS Records for a given domain."""
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    domain = request.GET.get('domain', '').strip().lower()
    if not domain:
        return redirect(reverse('sender_verify'))

    user_id = get_user_id(request)

    from Email_validate_app.models import SenderDomain
    from Email_validate_app.services.providers.ses_provider import SESProvider
    from Email_validate_app.services.providers.mailgun_provider import MailgunProvider

    db_row = SenderDomain.objects.filter(domain=domain, user_id=user_id, is_hidden=False).first()
    if not db_row:
        messages.warning(request, f'Domain "{domain}" not found in your account.')
        return redirect(reverse('sender_verify'))

    ses_records = (db_row.ses_dkim_tokens or [])
    mg_records  = (db_row.mailgun_dkim_tokens or [])

    # Fallback: fetch from providers if empty, then persist
    save_fields = []
    if not ses_records:
        try:
            ses_records = SESProvider().create_domain(domain).get('dns_records', [])
            if ses_records:
                db_row.ses_dkim_tokens = ses_records
                save_fields.append('ses_dkim_tokens')
        except Exception:
            pass
    if not mg_records:
        try:
            mg_records = MailgunProvider().create_domain(domain).get('dns_records', [])
            if mg_records:
                db_row.mailgun_dkim_tokens = mg_records
                save_fields.append('mailgun_dkim_tokens')
        except Exception:
            pass
    if save_fields:
        db_row.save(update_fields=save_fields)

    return render(request, 'i_Sender_Verify_DNS.html', {
        'domain':      domain,
        'ses_records': ses_records,
        'mg_records':  mg_records,
        'db_row':      db_row,
    })
