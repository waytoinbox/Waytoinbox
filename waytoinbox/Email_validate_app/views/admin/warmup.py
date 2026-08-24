"""
Warmup Receiver Pool — admin console page for managing the single, fixed,
shared pool of Gmail receiver accounts every user's warmup senders draw from.

End users never connect or manage receivers themselves (see
services/warmup.py::create_pending_messages_for_sender, which selects from
this pool with no per-user scoping at all) — only an admin, from here, runs
the Google OAuth consent flow that adds a receiver to the pool.
"""
import logging
import os

from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.timezone import now
from django.views.decorators.http import require_POST

from Email_validate_app.views.admin._base import admin_required, handle_admin_errors, audit

logger = logging.getLogger('Email_validate_app.views')


def oauth_client_config():
    """Returns (client_id, client_secret, redirect_uri) or (None, None, None)
    if not configured — a Web application OAuth client, distinct from the
    Desktop-app type used by the reference script this was ported from."""
    client_id     = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip()
    client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
    redirect_uri  = os.environ.get('GOOGLE_OAUTH_REDIRECT_URI', '').strip()
    if not client_id or not client_secret or not redirect_uri:
        return None, None, None
    return client_id, client_secret, redirect_uri


def _flow_client_config(client_id, client_secret, redirect_uri):
    return {
        'web': {
            'client_id':     client_id,
            'client_secret': client_secret,
            'auth_uri':      'https://accounts.google.com/o/oauth2/auth',
            'token_uri':     'https://oauth2.googleapis.com/token',
            'redirect_uris': [redirect_uri],
        }
    }


@admin_required
@handle_admin_errors
def admin_warmup_receivers(request):
    from Email_validate_app.models import WarmupReceiverAccount

    q      = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()

    qs = WarmupReceiverAccount.objects.filter(deleted_at__isnull=True).order_by('-created_at')
    if q:
        qs = qs.filter(email__icontains=q)
    if status:
        qs = qs.filter(status=status)

    total = qs.count()
    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1
    page_obj = Paginator(qs, 25).get_page(page_num)

    # Attach each visible receiver's most recent warmup message for a
    # "last received / landing / read" glance — current page only.
    from Email_validate_app.models import WarmupMessage
    for receiver in page_obj:
        receiver.last_message = WarmupMessage.objects.filter(
            receiver_account=receiver,
        ).order_by('-created_at').first()

    client_id, _, _ = oauth_client_config()

    return render(request, 'admin/warmup/index.html', {
        'page':             'warmup_receivers',
        'page_obj':         page_obj,
        'total':            total,
        'q':                q,
        'status':           status,
        'oauth_configured': bool(client_id),
    })


@admin_required
def admin_warmup_receiver_oauth_start(request):
    from google_auth_oauthlib.flow import Flow
    from Email_validate_app.services.warmup_receiver import SCOPES

    client_id, client_secret, redirect_uri = oauth_client_config()
    if not client_id:
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_not_configured')

    flow = Flow.from_client_config(
        _flow_client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES, redirect_uri=redirect_uri,
    )
    # access_type=offline + prompt=consent is what guarantees Google issues a
    # refresh_token, even if this Google account already granted this scope
    # to this app before.
    auth_url, state = flow.authorization_url(
        access_type='offline', prompt='consent', include_granted_scopes='true',
    )

    request.session['warmup_oauth_state'] = state
    return redirect(auth_url)


@admin_required
def admin_warmup_receiver_oauth_callback(request):
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

    from Email_validate_app.models import WarmupReceiverAccount
    from Email_validate_app.services.warmup_crypto import encrypt_token
    from Email_validate_app.services.warmup_receiver import SCOPES, get_profile_email

    state = request.session.get('warmup_oauth_state')
    if not state or request.GET.get('state') != state:
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_state_mismatch')

    client_id, client_secret, redirect_uri = oauth_client_config()
    if not client_id:
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_not_configured')

    flow = Flow.from_client_config(
        _flow_client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES, redirect_uri=redirect_uri, state=state,
    )
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception:
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_failed')

    credentials = flow.credentials
    if not credentials.refresh_token:
        # Google didn't issue one this time — ask the admin to revoke access
        # at myaccount.google.com/permissions and reconnect, since without a
        # refresh_token this receiver can never be checked unattended.
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_no_refresh_token')

    try:
        service = build('gmail', 'v1', credentials=credentials, cache_discovery=False)
        email = get_profile_email(service)
    except Exception:
        return redirect(reverse('admin_warmup_receivers') + '?error=oauth_profile_failed')

    receiver, _created = WarmupReceiverAccount.objects.update_or_create(
        email=email,
        defaults={
            'user':                    request._admin_user,
            'refresh_token_encrypted': encrypt_token(credentials.refresh_token),
            'status':                  'connected',
            'deleted_at':              None,
        },
    )
    audit(request, 'warmup_receiver.connect', 'warmup',
          target_type='warmup_receiver', target_id=receiver.id, target_repr=receiver.email)

    request.session.pop('warmup_oauth_state', None)
    return redirect(reverse('admin_warmup_receivers') + '?connected=1')


@admin_required
@handle_admin_errors
@require_POST
def admin_warmup_receiver_pause(request, rid):
    from Email_validate_app.models import WarmupReceiverAccount
    receiver = WarmupReceiverAccount.objects.filter(id=rid).first()
    if not receiver:
        return JsonResponse({'status': 'error', 'message': 'Receiver not found.'}, status=404)
    WarmupReceiverAccount.objects.filter(id=rid).update(status='paused')
    audit(request, 'warmup_receiver.pause', 'warmup',
          target_type='warmup_receiver', target_id=rid, target_repr=receiver.email)
    return JsonResponse({'status': 'ok'})


@admin_required
@handle_admin_errors
@require_POST
def admin_warmup_receiver_resume(request, rid):
    from Email_validate_app.models import WarmupReceiverAccount
    receiver = WarmupReceiverAccount.objects.filter(id=rid).first()
    if not receiver:
        return JsonResponse({'status': 'error', 'message': 'Receiver not found.'}, status=404)
    WarmupReceiverAccount.objects.filter(id=rid, status='paused').update(status='connected')
    audit(request, 'warmup_receiver.resume', 'warmup',
          target_type='warmup_receiver', target_id=rid, target_repr=receiver.email)
    return JsonResponse({'status': 'ok'})


@admin_required
@handle_admin_errors
@require_POST
def admin_warmup_receiver_delete(request, rid):
    from Email_validate_app.models import WarmupReceiverAccount
    receiver = WarmupReceiverAccount.objects.filter(id=rid).first()
    if not receiver:
        return JsonResponse({'status': 'error', 'message': 'Receiver not found.'}, status=404)
    WarmupReceiverAccount.objects.filter(id=rid).update(deleted_at=now())
    audit(request, 'warmup_receiver.delete', 'warmup',
          target_type='warmup_receiver', target_id=rid, target_repr=receiver.email)
    return JsonResponse({'status': 'ok', 'message': f'Removed {receiver.email} from the pool.'})
