import json
import logging

from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.timezone import now

from Email_validate_app.utils import get_user_id
from Email_validate_app.services.filter_utils import apply_search, apply_date_range, safe_date_range

logger = logging.getLogger(__name__)

# Top-level, single-select view (the 3 sidebar buttons) — everything else
# (status, classification, account, campaign) is an independent, combinable
# multi-select filter layered on top, not a folder value anymore.
FOLDER_CHOICES = ('all', 'primary', 'others')
# Primary = tied to a campaign contact OR a known SOProspect (even without an
# active campaign enrollment) — confirmed with the user as the inclusive
# reading of "prospect/campaign-related". Others = the negation.
_PRIMARY_Q = Q(campaign_contact__isnull=False) | Q(prospect__isnull=False)

# Status multi-select — every value ORs together; 'archived' is the one
# exception (see _apply_statuses) since is_archived is otherwise an implicit
# baseline exclusion, not a value the other statuses vary over.
STATUS_CHOICES = ('unread', 'needs_reply', 'waiting', 'sent', 'archived')
_STATUS_Q = {
    'unread':      Q(is_unread=True),
    'needs_reply': Q(last_message_direction='inbound'),
    'waiting':     Q(last_message_direction='outbound'),
    'sent':        Q(messages__direction='outbound'),
}

# All 9 SOConversation.CLASSIFICATION_CHOICES values, surfaced as a
# multi-select filter — the Classification dropdown lists exactly these.
CLASSIFICATION_CHOICES = {
    'interested': 'interested', 'meeting': 'meeting', 'question': 'question',
    'not_interested': 'not_interested', 'out_of_office': 'out_of_office',
    'unsubscribed': 'unsubscribe', 'wrong_person': 'wrong_person',
    'positive': 'positive', 'negative': 'negative',
}


def _parse_multi(request, param):
    raw = request.GET.get(param, '').strip()
    return [v for v in (p.strip() for p in raw.split(',')) if v] if raw else []


def _auth(request):
    if not request.session.get('logged_in'):
        return redirect(reverse('login'))


def _auth_json(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)


def _base_qs(user_id):
    from Email_validate_app.models import SOConversation
    # Account-scoped, not campaign-scoped — a conversation no longer requires a
    # campaign enrollment. Safe: SOEmailAccount is only ever soft-deleted in
    # this codebase, never hard-deleted, so account is always populated.
    return SOConversation.objects.filter(account__user_id=user_id)


def _apply_folder(qs, folder):
    if folder == 'primary':
        return qs.filter(_PRIMARY_Q)
    if folder == 'others':
        return qs.exclude(_PRIMARY_Q)
    return qs  # 'all' or unset


def _apply_statuses(qs, statuses):
    """Multi-select OR across statuses. 'archived' is handled separately from
    the other four: normally is_archived=False is an implicit baseline (an
    archived conversation doesn't show up just because it's also, say,
    unread), but ticking Archived explicitly should surface archived
    conversations too — so with no statuses picked, or only non-archived ones
    picked, archived stays excluded; the moment 'archived' itself is picked,
    every archived conversation is included regardless of the other ticks."""
    include_archived = 'archived' in statuses
    other = [s for s in statuses if s in _STATUS_Q]

    if not other:
        return qs.filter(is_archived=True) if include_archived else qs.filter(is_archived=False)

    combined = Q()
    for s in other:
        combined |= _STATUS_Q[s]
    q = Q(is_archived=False) & combined
    if include_archived:
        q |= Q(is_archived=True)
    return qs.filter(q).distinct()


def _apply_classifications(qs, classifications):
    mapped = [CLASSIFICATION_CHOICES[c] for c in classifications if c in CLASSIFICATION_CHOICES]
    if not mapped:
        return qs
    return qs.filter(classification__in=mapped)


def _folder_counts(qs):
    """All/Primary/Others counts — always computed against the un-archived
    baseline (matching how 'archived' living outside the folder buttons has
    behaved from the start: archived conversations don't inflate All/Primary/
    Others unless the Archived status is explicitly ticked)."""
    base = qs.filter(is_archived=False)
    return {folder: _apply_folder(base, folder).count() for folder in FOLDER_CHOICES}


def _status_counts(qs):
    """Faceted counts for the Status panel — each option counted against the
    queryset with every OTHER active filter applied but NOT the status
    filter itself, so ticking one status doesn't collapse its siblings'
    counts to zero."""
    counts = {}
    for s in STATUS_CHOICES:
        counts[s] = _apply_statuses(qs, [s]).count()
    return counts


def _classification_counts(qs):
    counts = {}
    for c in CLASSIFICATION_CHOICES:
        counts[c] = _apply_classifications(qs, [c]).count()
    return counts


def _prospect_name(conv):
    p = conv.prospect
    if p and (p.first_name or p.last_name):
        return f'{p.first_name or ""} {p.last_name or ""}'.strip()
    return conv.email.split('@')[0]


def _serialize_conversation(conv):
    cc = conv.campaign_contact
    campaign = conv.campaign
    return {
        'id': conv.id,
        'prospect_id': conv.prospect_id,
        'prospect_name': _prospect_name(conv),
        'company': (conv.prospect.company if conv.prospect else '') or '',
        'email': conv.email,
        'subject': conv.subject,
        'last_message_preview': conv.last_message_preview,
        'last_message_at': conv.last_message_at.isoformat() if conv.last_message_at else None,
        'last_message_direction': conv.last_message_direction,
        'is_unread': conv.is_unread,
        'is_archived': conv.is_archived,
        'classification': conv.classification,
        'campaign_id': conv.campaign_id,
        'campaign_name': campaign.name if campaign else None,
        'account_id': conv.account_id,
        'account_email': conv.account.email if conv.account else None,
        'current_step': cc.current_step if cc else 0,
        'sequence_status': cc.status if cc else '',
        'is_primary': bool(conv.campaign_contact_id or conv.prospect_id),
        'message_count': getattr(conv, 'message_count', None),
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in conv.tags.all()],
    }


# ── Page shell ───────────────────────────────────────────────────────────────

def so_inbox(request):
    r = _auth(request)
    if r:
        return r

    from Email_validate_app.models import SOCampaign, SOEmailAccount, SOTag
    user_id = get_user_id(request)

    campaigns = list(SOCampaign.objects.filter(user_id=user_id, deleted_at__isnull=True)
                      .order_by('-created_at').values('id', 'name'))
    accounts = list(SOEmailAccount.objects.filter(user_id=user_id, deleted_at__isnull=True)
                     .order_by('email').values('id', 'email'))
    tags = list(SOTag.objects.filter(user_id=user_id).values('id', 'name', 'color'))

    return render(request, 'i_SO_Inbox.html', {
        'campaigns': campaigns,
        'accounts':  accounts,
        'tags':      tags,
    })


# ── Conversation list (AJAX) ────────────────────────────────────────────────

def so_inbox_conversations(request):
    r = _auth_json(request)
    if r:
        return r

    user_id = get_user_id(request)
    folder          = request.GET.get('folder', 'all').strip()
    account_ids     = _parse_multi(request, 'account_ids')
    campaign_ids    = _parse_multi(request, 'campaign_ids')
    statuses        = _parse_multi(request, 'statuses')
    classifications = _parse_multi(request, 'classifications')
    tag_id     = request.GET.get('tag_id', '').strip()
    search     = request.GET.get('q', '').strip()
    date_from, date_to = safe_date_range(request.GET.get('date_from', ''), request.GET.get('date_to', ''))
    has_attachments = request.GET.get('has_attachments', '').strip() == '1'
    sort = request.GET.get('sort', 'recent').strip()
    page = request.GET.get('page', 1)

    scoped = _base_qs(user_id)
    if account_ids:
        scoped = scoped.filter(account_id__in=account_ids)
    if campaign_ids:
        scoped = scoped.filter(campaign_id__in=campaign_ids)
    counts = _folder_counts(scoped)

    scoped = _apply_folder(scoped, folder)
    # Faceted: each panel's counts reflect every OTHER active filter (account,
    # campaign, folder, and the opposite panel's current picks) but not its
    # own picks — so ticking "Interested" doesn't collapse every other
    # classification's count to zero.
    status_counts         = _status_counts(_apply_classifications(scoped, classifications))
    classification_counts = _classification_counts(_apply_statuses(scoped, statuses))

    qs = _apply_statuses(scoped, statuses)
    qs = _apply_classifications(qs, classifications)
    if tag_id:
        qs = qs.filter(tags__id=tag_id)
    if search:
        qs = apply_search(qs, search, 'email', 'subject', 'prospect__first_name',
                          'prospect__last_name', 'prospect__company', 'last_message_preview')
    qs = apply_date_range(qs, date_from, date_to, field='last_message_at')
    if has_attachments:
        from Email_validate_app.models import SOMessage
        # Exists(), not .filter(messages__has_attachments=True) — filtering and
        # annotating (message_count, below) over the same reverse relation in
        # one query makes Django reuse the JOIN, silently corrupting the count.
        qs = qs.filter(Exists(SOMessage.objects.filter(conversation=OuterRef('pk'), has_attachments=True)))

    qs = qs.select_related('campaign', 'account', 'prospect', 'campaign_contact').prefetch_related('tags')
    qs = qs.annotate(message_count=Count('messages', distinct=True))
    order = ('last_message_at', 'created_at') if sort == 'oldest' else ('-last_message_at', '-created_at')
    qs = qs.order_by(*order)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(page)

    return JsonResponse({
        'status': 'ok',
        'conversations': [_serialize_conversation(c) for c in page_obj],
        'counts': counts,
        'status_counts': status_counts,
        'classification_counts': classification_counts,
        'page': page_obj.number,
        'num_pages': paginator.num_pages,
        'has_next': page_obj.has_next(),
        'total': paginator.count,
    })


# ── Thread view (AJAX) ──────────────────────────────────────────────────────

def so_inbox_thread(request, conversation_id):
    r = _auth_json(request)
    if r:
        return r

    from Email_validate_app.services.so_inbox import get_thread_timeline
    user_id = get_user_id(request)
    conv = _base_qs(user_id).select_related(
        'campaign', 'account', 'prospect', 'campaign_contact'
    ).prefetch_related('tags').annotate(
        message_count=Count('messages', distinct=True)
    ).filter(id=conversation_id).first()
    if not conv:
        return JsonResponse({'status': 'error', 'message': 'Conversation not found.'}, status=404)

    if conv.is_unread:
        conv.__class__.objects.filter(id=conv.id).update(is_unread=False)
        conv.is_unread = False

    cc = conv.campaign_contact
    campaign = conv.campaign
    prospect = conv.prospect

    prospect_url = None
    campaign_url = None
    try:
        if prospect:
            prospect_url = reverse('so_prospect_detail', args=[prospect.id])
        if campaign:
            campaign_url = reverse('so_campaign_detail', args=[campaign.id])
    except Exception:
        pass

    return JsonResponse({
        'status': 'ok',
        'conversation': _serialize_conversation(conv),
        'prospect': {
            'id': prospect.id, 'first_name': prospect.first_name, 'last_name': prospect.last_name,
            'email': prospect.email, 'phone': prospect.phone, 'company': prospect.company,
            'status': prospect.status,
        } if prospect else None,
        'campaign': {'id': campaign.id, 'name': campaign.name, 'status': campaign.status} if campaign else None,
        'sequence': {
            'current_step': cc.current_step,
            'total_steps': campaign.steps.count(),
            'status': cc.status,
        } if (cc and campaign) else None,
        'prospect_url': prospect_url,
        'campaign_url': campaign_url,
        'timeline': get_thread_timeline(conv),
    })


# ── Reply (multipart POST — supports attachments) ───────────────────────────

def so_inbox_reply(request):
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.services.so_inbox import send_reply
    user_id = get_user_id(request)
    conversation_id = request.POST.get('conversation_id')
    body_html = request.POST.get('body_html', '').strip()
    forward = request.POST.get('forward', '') == '1'
    to_email = request.POST.get('to_email', '').strip()
    subject = request.POST.get('subject', '').strip()
    cc_email = request.POST.get('cc_email', '').strip()
    bcc_email = request.POST.get('bcc_email', '').strip()

    conv = _base_qs(user_id).select_related('account', 'campaign_contact').filter(id=conversation_id).first()
    if not conv:
        return JsonResponse({'status': 'error', 'message': 'Conversation not found.'}, status=404)
    if not body_html:
        return JsonResponse({'status': 'error', 'message': 'Message body is required.'}, status=400)
    if not to_email or '@' not in to_email:
        return JsonResponse({'status': 'error', 'message': 'Enter a valid recipient.'}, status=400)

    attachments = []
    for f in request.FILES.getlist('attachments')[:5]:
        attachments.append((f.name, f.read(), f.content_type or 'application/octet-stream'))

    try:
        message = send_reply(
            conv, body_html, attachments=attachments or None,
            to_email=to_email, forward=forward,
            subject=subject, cc_email=cc_email, bcc_email=bcc_email,
        )
    except Exception as exc:
        logger.warning('so_inbox: reply send failed for conversation %s: %s', conversation_id, exc)
        return JsonResponse({'status': 'error', 'message': str(exc)}, status=502)

    return JsonResponse({'status': 'ok', 'message_id': message.id, 'sent_at': message.sent_at.isoformat()})


def so_inbox_compose(request):
    """Send a brand-new outbound message not tied to an existing conversation
    (the Compose button) — finds or creates the conversation it belongs to,
    then sends through the same send_reply() SMTP path as a reply/forward."""
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOEmailAccount
    from Email_validate_app.services.so_inbox import get_or_create_compose_conversation, send_reply

    user_id = get_user_id(request)
    account_id = request.POST.get('account_id')
    to_email = request.POST.get('to_email', '').strip()
    subject = request.POST.get('subject', '').strip()
    cc_email = request.POST.get('cc_email', '').strip()
    bcc_email = request.POST.get('bcc_email', '').strip()
    body_html = request.POST.get('body_html', '').strip()

    account = SOEmailAccount.objects.filter(id=account_id, user_id=user_id, deleted_at__isnull=True).first()
    if not account:
        return JsonResponse({'status': 'error', 'message': 'Select a sender account.'}, status=400)
    if not to_email or '@' not in to_email:
        return JsonResponse({'status': 'error', 'message': 'Enter a valid recipient.'}, status=400)
    if not subject:
        return JsonResponse({'status': 'error', 'message': 'Subject is required.'}, status=400)
    if not body_html:
        return JsonResponse({'status': 'error', 'message': 'Message body is required.'}, status=400)

    attachments = []
    for f in request.FILES.getlist('attachments')[:5]:
        attachments.append((f.name, f.read(), f.content_type or 'application/octet-stream'))

    conversation = get_or_create_compose_conversation(account, to_email, subject)
    try:
        message = send_reply(
            conversation, body_html, attachments=attachments or None,
            to_email=to_email, forward=False,
            subject=subject, cc_email=cc_email, bcc_email=bcc_email,
        )
    except Exception as exc:
        logger.warning('so_inbox: compose send failed for account %s: %s', account_id, exc)
        return JsonResponse({'status': 'error', 'message': str(exc)}, status=502)

    return JsonResponse({'status': 'ok', 'conversation_id': conversation.id, 'message_id': message.id})


def so_inbox_upload_image(request):
    """Inline image upload for the composer's Quill editor (Insert image
    toolbar button). Deliberately its own endpoint rather than reusing Email
    Marketing's upload_template_image/TemplateImage — SO stays isolated from
    Email Marketing, no shared tables (see models.py's SOConversation
    docstring). Just saves to default_storage; no DB row needed since nothing
    else ever needs to look this upload back up."""
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    file = request.FILES.get('image')
    if not file:
        return JsonResponse({'status': 'error', 'message': 'No image provided.'}, status=400)
    if file.size > 5 * 1024 * 1024:
        return JsonResponse({'status': 'error', 'message': 'Image too large (max 5 MB).'}, status=400)

    # Verify actual file content — client-supplied Content-Type can be spoofed.
    try:
        import io
        from PIL import Image
        Image.open(io.BytesIO(file.read())).verify()
        file.seek(0)
    except Exception:
        return JsonResponse({'status': 'error', 'message': 'Invalid image file.'}, status=400)

    import os
    import uuid
    from django.core.files.storage import default_storage

    user_id = get_user_id(request)
    ext = os.path.splitext(file.name)[1][:10]
    path = default_storage.save(f'so_inbox_uploads/{user_id}/{uuid.uuid4().hex}{ext}', file)
    return JsonResponse({'status': 'ok', 'url': default_storage.url(path)})


# ── Notes ────────────────────────────────────────────────────────────────────

def so_inbox_note_add(request):
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import UserTable, SOConversationNote
    user_id = get_user_id(request)
    data = json.loads(request.body)
    body = (data.get('body') or '').strip()
    conversation_id = data.get('conversation_id')

    conv = _base_qs(user_id).filter(id=conversation_id).first()
    if not conv:
        return JsonResponse({'status': 'error', 'message': 'Conversation not found.'}, status=404)
    if not body:
        return JsonResponse({'status': 'error', 'message': 'Note body is required.'}, status=400)

    note = SOConversationNote.objects.create(conversation=conv, user_id=user_id, body=body)
    return JsonResponse({'status': 'ok', 'note_id': note.id, 'created_at': note.created_at.isoformat()})


# ── Actions — single or bulk ─────────────────────────────────────────────────

def so_inbox_action(request):
    r = _auth_json(request)
    if r:
        return r
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    from Email_validate_app.models import SOConversation, SOProspect, SOTag
    from Email_validate_app.services import so_drip
    from Email_validate_app.services.so_inbox import unsubscribe_contact

    user_id = get_user_id(request)
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)

    action = data.get('action')
    ids = data.get('conversation_ids')
    if ids is None:
        cid = data.get('conversation_id')
        ids = [cid] if cid else []
    value = data.get('value')

    convs = list(_base_qs(user_id).select_related('campaign_contact').filter(id__in=ids))
    if not convs:
        return JsonResponse({'status': 'error', 'message': 'No conversations found.'}, status=404)

    updated = 0
    for conv in convs:
        if action == 'mark_read':
            SOConversation.objects.filter(id=conv.id).update(is_unread=False)
        elif action == 'mark_unread':
            SOConversation.objects.filter(id=conv.id).update(is_unread=True)
        elif action == 'archive':
            SOConversation.objects.filter(id=conv.id).update(is_archived=True)
        elif action == 'unarchive':
            SOConversation.objects.filter(id=conv.id).update(is_archived=False)
        elif action == 'pause_sequence':
            if conv.campaign_contact:
                so_drip.stop(conv.campaign_contact, 'manual')
        elif action == 'resume_sequence':
            if conv.campaign_contact:
                so_drip.resume(conv.campaign_contact)
        elif action == 'classify':
            if value in dict(SOConversation.CLASSIFICATION_CHOICES) or value == '':
                SOConversation.objects.filter(id=conv.id).update(classification=value)
        elif action == 'add_tag':
            name = (value or '').strip()
            if name:
                tag, _ = SOTag.objects.get_or_create(user_id=user_id, name=name)
                conv.tags.add(tag)
        elif action == 'remove_tag':
            conv.tags.remove(value)
        elif action == 'unsubscribe':
            if conv.campaign_contact:
                unsubscribe_contact(conv.campaign_contact)
            elif conv.prospect_id:
                SOProspect.objects.filter(id=conv.prospect_id, deleted_at__isnull=True).update(
                    status='unsubscribed'
                )
            SOConversation.objects.filter(id=conv.id).update(classification='unsubscribe')
        else:
            return JsonResponse({'status': 'error', 'message': f'Unknown action "{action}".'}, status=400)
        updated += 1

    return JsonResponse({'status': 'ok', 'updated': updated})
