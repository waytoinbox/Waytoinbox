import json
import smtplib
import ssl

from datetime import timedelta

from django.core import signing
from django.db.models import Count
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
    from Email_validate_app.models import (
        SOEmailAccount, SOEmailAccountDailyUsage, SOEmailAccountRotation,
        SOCampaignContact, SOEvent,
    )
    user_id  = get_user_id(request)
    accounts = list(SOEmailAccount.objects.filter(
        user_id=user_id, deleted_at__isnull=True,
    ).select_related('warmup'))
    account_ids = [a.id for a in accounts]

    # Today's sent count against each account's daily_limit (e.g. "35/50") —
    # same UTC-day counter services/so_drip.py reserves against before a send,
    # so this reflects the exact quota the drip dispatcher is enforcing.
    today = now().date()
    usage_map = dict(
        SOEmailAccountDailyUsage.objects.filter(
            account_id__in=account_ids, date=today,
        ).values_list('account_id', 'sent_count')
    )

    # Campaigns count — distinct non-deleted campaigns this account is
    # configured as a sender for (SOEmailAccountRotation is written whenever
    # an account is selected in the wizard's Sender Accounts step, regardless
    # of whether any contact has actually been sent to yet).
    campaigns_map = dict(
        SOEmailAccountRotation.objects.filter(
            account_id__in=account_ids, campaign__deleted_at__isnull=True,
        ).values('account_id').annotate(n=Count('campaign_id', distinct=True))
        .values_list('account_id', 'n')
    )

    # Reply / prospects counts for the last 7 days — the numerator/denominator
    # of a per-account reply rate. Prospects = total campaign contacts this
    # account actually sent to (SOCampaignContact.account is the sticky
    # per-contact sender assignment, sent_at marks an actual send — one row
    # per (campaign, contact), so this is a per-campaign count, not a
    # per-person one); replies = total 'replied' SOEvents attributed to this
    # account (SOEvent's own (campaign_id, email, event_type) dedupe already
    # guarantees at most one 'replied' row per contact per campaign — see
    # services/so_imap.py::_record_once — so this counts "how many of this
    # account's campaign sends got a reply", not distinct people). Deliberately
    # NOT distinct-by-email: the same prospect enrolled in 3 campaigns who
    # replied to all 3 must show as 3 replies / 3 prospects, not 1/1.
    cutoff_7d = now() - timedelta(days=7)
    prospects_map = dict(
        SOCampaignContact.objects.filter(
            account_id__in=account_ids, sent_at__gte=cutoff_7d,
        ).values('account_id').annotate(n=Count('id'))
        .values_list('account_id', 'n')
    )
    replies_map = dict(
        SOEvent.objects.filter(
            account_id__in=account_ids, event_type='replied', created_at__gte=cutoff_7d,
        ).values('account_id').annotate(n=Count('id'))
        .values_list('account_id', 'n')
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
        acc.campaigns_count = campaigns_map.get(acc.id, 0)
        acc.replies_7d      = replies_map.get(acc.id, 0)
        acc.prospects_7d    = prospects_map.get(acc.id, 0)

    return render(request, 'i_SO_Email_Accounts.html', {'accounts': accounts})


def so_add_email_account(request):
    r = _auth(request)
    if r:
        return r
    return render(request, 'i_SO_Add_Email_Account.html')


def so_edit_email_account(request, id):
    """Standalone Edit Email Account page (V4.8) — replaces the former
    in-page Edit modal. Renders the account's current details for editing;
    Save Changes posts to the existing so_email_account_action's `edit`
    action (unchanged), so no backend edit logic was duplicated here."""
    r = _auth(request)
    if r:
        return r
    from Email_validate_app.models import SOEmailAccount
    user_id = get_user_id(request)
    try:
        acc = SOEmailAccount.objects.select_related('warmup').get(
            id=id, user_id=user_id, deleted_at__isnull=True,
        )
    except SOEmailAccount.DoesNotExist:
        return redirect(reverse('so_email_accounts'))
    return render(request, 'i_SO_Edit_Email_Account.html', {'acc': acc})


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

        existing_count = SOEmailAccount.objects.filter(
            user_id=user_id,
            deleted_at__isnull=True,
        ).count()

        if existing_count >= 2:
            return JsonResponse({
                'status': 'error',
                'message': 'You can add up to 2 Sales Outreach email accounts only.'
            })

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

    # ── Edit (V4.5 — account details / sending / warmup settings) ─────────────
    # Deliberately allowlists exactly display_name/daily_limit/warmup.* —
    # connection identity fields (email, provider, smtp/imap host+port,
    # username, password) are never read from this payload at all, so
    # there is no field to accidentally overwrite even if a caller sent one.
    if action == 'edit':
        acc_id = data.get('id')
        try:
            acc = SOEmailAccount.objects.get(id=acc_id, user_id=user_id, deleted_at__isnull=True)
        except SOEmailAccount.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Account not found.'})

        errors = {}

        display_name = data.get('display_name')
        if display_name is not None:
            display_name = display_name.strip()

        daily_limit = None
        try:
            daily_limit = int(data.get('daily_limit'))
            if daily_limit <= 0:
                errors['daily_limit'] = 'Daily sending limit must be greater than 0.'
        except (TypeError, ValueError):
            errors['daily_limit'] = 'Daily sending limit must be a whole number.'

        # warmup settings are entirely optional in the payload — the
        # frontend only sends them when this account actually has a
        # warmup row (see so_email_account_action's caller); still
        # validated defensively here regardless.
        warmup_payload = data.get('warmup') or {}
        warmup_updates = {}
        for key, label in (
            ('daily_target', 'Daily target'),
            ('ramp_up_days', 'Ramp-up days'),
            ('ramp_up_increment', 'Daily increment'),
        ):
            if key not in warmup_payload:
                continue
            try:
                val = int(warmup_payload.get(key))
                if val <= 0:
                    errors[key] = f'{label} must be greater than 0.'
                else:
                    warmup_updates[key] = val
            except (TypeError, ValueError):
                errors[key] = f'{label} must be a whole number.'

        if errors:
            return JsonResponse({'status': 'error', 'errors': errors})

        update_fields = ['daily_limit', 'updated_at']
        acc.daily_limit = daily_limit
        if display_name is not None:
            acc.display_name = display_name
            update_fields.append('display_name')
        acc.save(update_fields=update_fields)

        warmup_result = None
        if warmup_updates:
            from Email_validate_app.services.warmup import update_warmup_settings
            warmup_obj = update_warmup_settings(acc, **warmup_updates)
            if warmup_obj:
                warmup_result = {
                    'daily_target':      warmup_obj.daily_target,
                    'ramp_up_days':      warmup_obj.ramp_up_days,
                    'ramp_up_increment': warmup_obj.ramp_up_increment,
                    'status':            warmup_obj.status,
                    'status_display':    warmup_obj.get_status_display(),
                }

        return JsonResponse({
            'status': 'ok',
            'account': {'id': acc.id, 'display_name': acc.display_name, 'daily_limit': acc.daily_limit},
            'warmup': warmup_result,
        })

    return JsonResponse({'status': 'error', 'message': 'Unknown action.'})
