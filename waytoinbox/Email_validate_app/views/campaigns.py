import json
import re

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.core.paginator import Paginator
from django.utils import timezone
from django.views.decorators.http import require_POST as _require_POST

from Email_validate_app.utils import get_user_id
from Email_validate_app.services.filter_utils import extract_filter_params, apply_search, apply_status, apply_date_range
from Email_validate_app.services.filter_status import CAMPAIGN_STATUSES, CAMPAIGN_EVENT_STATUSES

from .contacts import _sync_campaign_list_counts


def campaigns(request):
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    from datetime import timedelta
    from Email_validate_app.models import Campaign
    user_id = get_user_id(request)
    PAGE_SIZE = getattr(settings, 'FILTER_DEFAULT_PAGE_SIZE', 25)

    f = extract_filter_params(request)

    qs = Campaign.objects.select_related('campaign_list').filter(
        user_id=user_id, deleted_at__isnull=True,
    ).order_by('-created_at')

    qs = apply_search(qs, f['search'], 'campaign_name')
    qs = apply_status(qs, f['status'])
    qs = apply_date_range(qs, f['date_from'], f['date_to'])

    if request.GET.get('format') == 'json':
        rows = list(qs.values('Campaign_ID', 'status'))
        return JsonResponse({'campaigns': rows})

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    for c in page_obj.object_list:
        c.is_editable = c.status in ('draft', 'scheduled', 'failed')
        c.was_edited  = c.updated_at - c.created_at > timedelta(minutes=1)

    return render(request, 'i_Campaigns.html', {
        'page_obj':    page_obj,
        'total_count': paginator.count,
        'search':    f['search'],
        'status':    f['status'],
        'date_from': str(f['date_from']) if f['date_from'] else '',
        'date_to':   str(f['date_to']) if f['date_to'] else '',
        'pf_statuses': CAMPAIGN_STATUSES,
        'pf_show_status': True,
        'pf_search_placeholder': 'Search campaigns…',
    })


def campaign_detail(request, campaign_id):
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    from Email_validate_app.models import Campaign, CampaignEvent, CampaignStats
    user_id = get_user_id(request)

    try:
        campaign = Campaign.objects.select_related(
            'campaign_list', 'template', 'user'
        ).get(Campaign_ID=campaign_id, user_id=user_id, deleted_at__isnull=True)
    except Campaign.DoesNotExist:
        messages.error(request, "Campaign not found.")
        return redirect(reverse('campaigns'))

    try:
        stats = campaign.stats
    except CampaignStats.DoesNotExist:
        stats = None

    # Build per-email event presence map from CampaignEvent rows
    _ORDERED_TYPES = ['send', 'delivery', 'open', 'click', 'bounce', 'complaint', 'reject', 'unsubscribe']
    _EVENT_PRIORITY = {et: i for i, et in enumerate(_ORDERED_TYPES)}  # lower = higher priority

    events_qs = (
        CampaignEvent.objects
        .filter(campaign=campaign)
        .order_by('event_time')
        .values('email', 'event_type', 'event_time')
    )

    email_map = {}  # email -> {event_type: True, '_last_event': str, '_last_time': datetime}
    for ev in events_qs:
        addr = ev['email']
        if addr not in email_map:
            email_map[addr] = {'_last_event': None, '_last_time': None}
        email_map[addr][ev['event_type']] = True
        if email_map[addr]['_last_time'] is None or ev['event_time'] > email_map[addr]['_last_time']:
            email_map[addr]['_last_event'] = ev['event_type']
            email_map[addr]['_last_time'] = ev['event_time']

    recipient_rows = []
    for addr, data in email_map.items():
        row = {'email': addr, 'last_event': data['_last_event']}
        for et in _ORDERED_TYPES:
            row[et] = bool(data.get(et))
        recipient_rows.append(row)

    # Sort: worst outcome first (bounce/complaint), then by send->delivery->open->click
    recipient_rows.sort(key=lambda r: _EVENT_PRIORITY.get(r['last_event'] or '', 99))

    def _pct(num, den):
        if not den:
            return None
        v = round(num / den * 100, 1)
        return int(v) if v == int(v) else v

    total       = campaign.total_recipients or 0
    s_sent      = (stats.total_sent        or 0) if stats else 0
    s_delivered = (stats.total_delivered   or 0) if stats else 0
    s_opened    = (stats.total_opened      or 0) if stats else 0
    s_clicked   = (stats.total_clicked     or 0) if stats else 0
    s_bounced   = (stats.total_bounced     or 0) if stats else 0
    s_unsub     = (stats.total_unsubscribed or 0) if stats else 0

    stat_pct = {
        'sent':         _pct(s_sent,      total),
        'delivered':    _pct(s_delivered, s_sent),
        'opened':       _pct(s_opened,    s_delivered),
        'clicked':      _pct(s_clicked,   s_delivered),
        'bounced':      _pct(s_bounced,   s_sent),
        'unsubscribed': _pct(s_unsub,     s_delivered),
    }

    return render(request, 'i_Campaign_Detail.html', {
        'campaign':       campaign,
        'stats':          stats,
        'stat_pct':       stat_pct,
        'recipient_rows': recipient_rows,
        'pf_statuses':    CAMPAIGN_EVENT_STATUSES,
        'pf_show_status': True,
    })


def campaign_stats_json(request, campaign_id):
    if not request.session.get('logged_in'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    from Email_validate_app.models import Campaign, CampaignStats
    user_id = get_user_id(request)

    try:
        campaign = Campaign.objects.get(
            Campaign_ID=campaign_id, user_id=user_id, deleted_at__isnull=True
        )
    except Campaign.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    try:
        stats = campaign.stats
    except CampaignStats.DoesNotExist:
        return JsonResponse({
            'status': campaign.status,
            'sent': 0, 'delivered': 0, 'opened': 0,
            'clicked': 0, 'bounced': 0, 'complaints': 0, 'unsubscribed': 0,
        })

    return JsonResponse({
        'status':       campaign.status,
        'sent':         stats.total_sent         or 0,
        'delivered':    stats.total_delivered    or 0,
        'opened':       stats.total_opened       or 0,
        'clicked':      stats.total_clicked      or 0,
        'bounced':      stats.total_bounced      or 0,
        'complaints':   stats.total_complaints   or 0,
        'unsubscribed': stats.total_unsubscribed or 0,
    })


@_require_POST
def save_campaign(request):
    """Save a campaign as a draft, or send/schedule it. Campaign.template
    always references a UserTemplate, never a TemplateLibrary record."""
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from django.utils import timezone
    from Email_validate_app.models import Campaign, CampaignList, UserTemplate, SenderEmailToken
    user_id = get_user_id(request)

    campaign_id    = request.POST.get('campaign_id', '').strip()
    action         = request.POST.get('action', 'draft')
    campaign_name  = request.POST.get('campaign_name', '').strip()
    recipient_type = request.POST.get('recipient_type', 'list').strip()
    list_id        = request.POST.get('campaign_list_id', '').strip()
    segment_id     = request.POST.get('segment_id', '').strip()
    # Multi-select: comma-separated IDs (new flow)
    list_ids_raw         = [x for x in request.POST.get('list_ids', '').split(',') if x.strip()]
    segment_ids_raw      = [x for x in request.POST.get('segment_ids', '').split(',') if x.strip()]
    excl_list_ids_raw    = [x for x in request.POST.get('exclude_list_ids', '').split(',') if x.strip()]
    excl_segment_ids_raw = [x for x in request.POST.get('exclude_segment_ids', '').split(',') if x.strip()]
    is_multi = bool(list_ids_raw or segment_ids_raw)
    template_id    = request.POST.get('template_id', '').strip()
    sender_name   = request.POST.get('sender_name', '').strip()
    from_email    = request.POST.get('from_email', '').strip()
    reply_email   = request.POST.get('reply_email', '').strip()
    email_subject = request.POST.get('email_subject', '').strip()
    send_option    = request.POST.get('send_option', 'now')     # 'now' | 'schedule'
    schedule_date  = request.POST.get('schedule_date', '').strip()  # 'YYYY-MM-DD'
    schedule_time  = request.POST.get('schedule_time', '').strip()  # 'HH:MM' 24-hr
    campaign_tz    = request.POST.get('campaign_tz', 'Asia/Kolkata').strip() or 'Asia/Kolkata'

    email_re = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

    errors = {}
    if not campaign_name:
        errors['campaign_name'] = 'Campaign name is required.'
    if is_multi:
        if not list_ids_raw and not segment_ids_raw:
            errors['campaign_list_id'] = 'Select at least one list or segment.'
    elif recipient_type == 'segment':
        if not segment_id:
            errors['campaign_list_id'] = 'Select a segment.'
    else:
        if not list_id:
            errors['campaign_list_id'] = 'Recipient list is required.'
    if not sender_name:
        errors['sender_name'] = 'Sender name is required.'
    if not from_email or not email_re.match(from_email):
        errors['from_email'] = 'A valid From email is required.'
    elif not SenderEmailToken.objects.filter(
        user_id=user_id, email__iexact=from_email, confirmed=True, is_hidden=False
    ).exists():
        errors['from_email'] = 'This email is not verified. Go to Sender Verify and verify it first.'
    if not reply_email or not email_re.match(reply_email):
        errors['reply_email'] = 'A valid Reply-To email is required.'
    if action == 'send' and not template_id:
        errors['template_id'] = 'Select an email template before sending.'

    from zoneinfo import ZoneInfo, available_timezones
    from datetime import datetime as _datetime, timezone as _datetime_timezone
    if campaign_tz not in available_timezones():
        campaign_tz = 'Asia/Kolkata'

    schedule_dt = None
    if send_option == 'schedule':
        if not schedule_date or not schedule_time:
            errors['schedule_at'] = 'Choose a date and time to schedule this campaign.'
        else:
            try:
                naive_dt  = _datetime.strptime(f'{schedule_date} {schedule_time}', '%Y-%m-%d %H:%M')
                local_dt  = naive_dt.replace(tzinfo=ZoneInfo(campaign_tz))
                schedule_dt = local_dt.astimezone(_datetime_timezone.utc)
                if schedule_dt <= timezone.now():
                    errors['schedule_at'] = 'Scheduled time must be in the future.'
            except (ValueError, KeyError):
                errors['schedule_at'] = 'Invalid date or time format.'

    if errors:
        return JsonResponse({'status': 'error', 'errors': errors}, status=400)

    from Email_validate_app.models import Segment
    from Email_validate_app.services.segment_builder import count_segment_contacts

    campaign_list    = None
    campaign_segment = None
    selected_lists    = []
    selected_segments = []

    if is_multi:
        selected_lists = list(
            CampaignList.objects.filter(id__in=list_ids_raw, user_id=user_id, deleted_at__isnull=True)
        )
        selected_segments = list(
            Segment.objects.filter(id__in=segment_ids_raw, user_id=user_id, deleted_at__isnull=True)
        )
        # backward-compat FK: first list or None
        campaign_list    = selected_lists[0]    if selected_lists    else None
        campaign_segment = selected_segments[0] if not selected_lists and selected_segments else None
        total_recipients = (
            sum(l.subscribed_count for l in selected_lists) +
            sum(count_segment_contacts(s, user_id) for s in selected_segments)
        )
    elif recipient_type == 'segment':
        try:
            campaign_segment = Segment.objects.get(id=segment_id, user_id=user_id, deleted_at__isnull=True)
            selected_segments = [campaign_segment]
        except Segment.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Segment not found.'}, status=404)
        total_recipients = count_segment_contacts(campaign_segment, user_id)
    else:
        try:
            campaign_list = CampaignList.objects.get(id=list_id, user_id=user_id, deleted_at__isnull=True)
            selected_lists = [campaign_list]
        except CampaignList.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Recipient list not found.'}, status=404)
        total_recipients = campaign_list.subscribed_count

    template = None
    if template_id:
        try:
            template = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
        except UserTemplate.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Template not found.'}, status=404)
        if email_subject and email_subject != template.subject:
            template.subject = email_subject
            template.save(update_fields=['subject'])

    if action == 'draft':
        status = 'draft'
    elif send_option == 'schedule':
        status = 'scheduled'
    else:
        status = 'sending'

    fields = dict(
        campaign_name=campaign_name,
        campaign_list=campaign_list,
        campaign_segment=campaign_segment,
        template=template,
        sender_name=sender_name,
        from_email=from_email,
        reply_email=reply_email,
        schedule_at=schedule_dt,
        schedule_timezone=campaign_tz,
        status=status,
        total_recipients=total_recipients,
    )
    # Clear any stale sent_at when (re-)scheduling or saving as draft
    if status in ('draft', 'scheduled'):
        fields['sent_at'] = None

    if campaign_id:
        try:
            campaign = Campaign.objects.get(Campaign_ID=campaign_id, user_id=user_id, deleted_at__isnull=True)
        except Campaign.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Campaign not found.'}, status=404)
        if campaign.status not in ('draft', 'scheduled', 'failed'):
            return JsonResponse({'status': 'error', 'message': 'Only draft, scheduled, or failed campaigns can be edited.'}, status=400)
        for k, v in fields.items():
            setattr(campaign, k, v)
        campaign.save()
    else:
        campaign = Campaign.objects.create(user_id=user_id, **fields)

    # Sync M2M recipients
    if selected_lists or selected_segments or is_multi:
        campaign.campaign_lists.set(selected_lists)
        campaign.campaign_segments.set(selected_segments)

    # Sync exclude M2M
    excl_lists    = list(CampaignList.objects.filter(id__in=excl_list_ids_raw, user_id=user_id, deleted_at__isnull=True))
    excl_segments = list(Segment.objects.filter(id__in=excl_segment_ids_raw, user_id=user_id, deleted_at__isnull=True))
    campaign.exclude_lists.set(excl_lists)
    campaign.exclude_segments.set(excl_segments)

    if status == 'sending':
        from Email_validate_app.models import CampaignEmail
        from Email_validate_app.services.credit_manager import get_cc_current_credit
        from Email_validate_app.tasks.send_scheduled_campaigns import send_campaign_emails_task

        if campaign.campaign_segment_id:
            from Email_validate_app.services.segment_builder import get_segment_emails
            all_emails = get_segment_emails(campaign.campaign_segment, campaign.user_id)
            recipient_count = CampaignEmail.objects.filter(
                user_id=campaign.user_id,
                email__in=all_emails,
                subscribed='subscribed',
                deleted_at__isnull=True,
            ).values('email').distinct().count()
        else:
            recipient_count = CampaignEmail.objects.filter(
                list_id=campaign.campaign_list_id,
                deleted_at__isnull=True,
                subscribed='subscribed',
            ).count()
        cc_available = get_cc_current_credit(user_id)
        if cc_available < recipient_count:
            campaign.status = 'draft'
            campaign.save(update_fields=['status'])
            return JsonResponse({
                'status': 'error',
                'message': (
                    f"Not enough Contact Credits. Need {recipient_count:,}, "
                    f"you have {cc_available:,}. Please upgrade your subscription."
                ),
            }, status=400)

        send_campaign_emails_task.delay(campaign.id)
        return JsonResponse({
            'status':          'ok',
            'campaign_id':     campaign.Campaign_ID,
            'campaign_status': 'sending',
            'redirect_url':    reverse('campaigns'),
        })

    return JsonResponse({
        'status':          'ok',
        'campaign_id':      campaign.Campaign_ID,
        'campaign_status':  campaign.status,
        'redirect_url':     reverse('campaigns'),
    })


@_require_POST
def send_test_email_create(request):
    """Send a test email from the Create Campaign page.

    Security model: the campaign is auto-saved as a draft on the first test
    send, giving a real campaign_id.  All subsequent attempts — regardless of
    browser, tab, or direct API call — are blocked server-side by checking
    CampaignTestSend for an existing success record tied to that campaign_id.
    The campaign_id is returned to the frontend so it can be reused on retry.
    """
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)
    try:
        return _send_test_email_create_inner(request)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JsonResponse(
            {'status': 'error', 'message': f'Server error: {exc}'},
            status=500,
        )


def _send_test_email_create_inner(request):

    import json, re
    from Email_validate_app.models import (
        Campaign, CampaignList, CampaignTestSend, UserTemplate,
    )
    user_id = get_user_id(request)

    campaign_id   = request.POST.get('campaign_id',   '').strip()
    campaign_name = request.POST.get('campaign_name', '').strip()
    list_id       = request.POST.get('campaign_list_id', '').strip()
    template_id   = request.POST.get('template_id',  '').strip()
    sender_name   = request.POST.get('sender_name',  '').strip()
    from_email    = request.POST.get('from_email',   '').strip()
    reply_email   = request.POST.get('reply_email',  '').strip()
    raw_emails    = request.POST.get('test_emails',  '').strip()

    email_re = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    errors = {}

    if not campaign_name:
        errors['campaign_name'] = 'Campaign name (Section 1) is required before sending a test.'
    if not template_id:
        errors['template_id'] = 'Select an email template (Section 2) before sending a test.'
    if not sender_name:
        errors['sender_name'] = 'Sender name (Section 3) is required.'
    if not from_email or not email_re.match(from_email):
        errors['from_email'] = 'A valid From email (Section 3) is required.'
    if not reply_email or not email_re.match(reply_email):
        errors['reply_email'] = 'A valid Reply-To email (Section 3) is required.'

    try:
        emails = json.loads(raw_emails)
        if not isinstance(emails, list):
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid email list.'}, status=400)

    emails = [e.strip() for e in emails if isinstance(e, str) and e.strip()]
    if not emails:
        errors['test_emails'] = 'Enter at least 1 recipient email address.'
    elif len(emails) > 5:
        errors['test_emails'] = 'Maximum 5 test recipients allowed.'
    else:
        bad = [e for e in emails if not email_re.match(e)]
        if bad:
            errors['test_emails'] = f'Invalid address(es): {", ".join(bad)}'

    if errors:
        return JsonResponse(
            {'status': 'error', 'message': next(iter(errors.values())), 'errors': errors},
            status=400,
        )

    try:
        template = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
    except UserTemplate.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Template not found.'}, status=404)

    # Resolve the list FK only if the user has already selected one
    campaign_list = None
    if list_id:
        try:
            campaign_list = CampaignList.objects.get(id=list_id, user_id=user_id, deleted_at__isnull=True)
        except CampaignList.DoesNotExist:
            pass  # not yet selected — allowed for test sends

    # Resolve campaign (fetch existing draft or auto-save a new one)
    if campaign_id:
        try:
            campaign = Campaign.objects.get(Campaign_ID=campaign_id, user_id=user_id, deleted_at__isnull=True)
        except Campaign.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Campaign not found.'}, status=404)
        if campaign.status not in ('draft', 'failed'):
            return JsonResponse(
                {'status': 'error', 'message': 'Only draft campaigns can send a test email.'},
                status=400,
            )
        update_fields = ['campaign_name', 'template', 'sender_name', 'from_email', 'reply_email']
        campaign.campaign_name = campaign_name
        campaign.template      = template
        campaign.sender_name   = sender_name
        campaign.from_email    = from_email
        campaign.reply_email   = reply_email
        if campaign_list:
            campaign.campaign_list = campaign_list
            update_fields.append('campaign_list')
        campaign.save(update_fields=update_fields)
    else:
        campaign = Campaign.objects.create(
            user_id=user_id,
            campaign_name=campaign_name,
            campaign_list=campaign_list,
            template=template,
            sender_name=sender_name,
            from_email=from_email,
            reply_email=reply_email,
            status='draft',
            total_recipients=campaign_list.total_count if campaign_list else 0,
        )

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    from Email_validate_app.services.campaign_sender import (
        build_unsubscribe_link as _build_unsubscribe_link,
        inject_unsubscribe_link as _inject_unsubscribe_link,
    )
    from Email_validate_app.services.providers import get_provider

    provider  = get_provider()
    source    = f"{sender_name} <{from_email}>"
    subject   = f"[TEST] {template.subject or campaign_name}"
    base_html = template.html_content

    sent, send_errors = 0, []
    for addr in emails:
        try:
            link = _build_unsubscribe_link(addr, campaign.id, test=True)
            html = _inject_unsubscribe_link(base_html, link)

            msg = MIMEMultipart('alternative')
            msg['From']                  = source
            msg['To']                    = addr
            msg['Subject']               = str(Header(subject, 'utf-8'))
            msg['Reply-To']              = reply_email
            msg['List-Unsubscribe']      = f'<{link}>'
            msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
            msg.attach(MIMEText(html, 'html', 'utf-8'))

            result = provider.send_raw(source, addr, msg.as_bytes(), tags={'test': '1'})
            if result.success:
                sent += 1
            else:
                send_errors.append(f'{addr}: {result.error}')
        except Exception as exc:
            send_errors.append(f'{addr}: {exc}')

    # Persist result
    test_status = 'success' if sent > 0 else 'failed'
    CampaignTestSend.objects.create(
        campaign=campaign,
        user_id=user_id,
        template=template,
        recipients=emails,
        sender_name=sender_name,
        from_email=from_email,
        reply_email=reply_email,
        status=test_status,
        error_log='; '.join(send_errors),
    )

    if sent > 0:
        return JsonResponse({
            'status':      'ok',
            'sent':        sent,
            'campaign_id': campaign.Campaign_ID,
            'message':     f'Test email sent to {sent} recipient(s).',
        })
    return JsonResponse(
        {
            'status':      'error',
            'campaign_id': campaign.Campaign_ID,
            'message':     'Failed to send: ' + '; '.join(send_errors),
        },
        status=500,
    )


@_require_POST
def send_test_email(request, campaign_id):
    """Send a test email for a campaign to 1-5 specified addresses."""
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    import json, re
    from Email_validate_app.models import Campaign, CampaignTestSend
    user_id = get_user_id(request)

    try:
        campaign = Campaign.objects.get(id=campaign_id, user_id=user_id, deleted_at__isnull=True)
    except Campaign.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Campaign not found.'}, status=404)

    if campaign.template is None:
        return JsonResponse(
            {'status': 'error', 'message': 'Add an email template to the campaign before sending a test.'},
            status=400,
        )

    raw = request.POST.get('test_emails', '').strip()
    try:
        emails = json.loads(raw)
        if not isinstance(emails, list):
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid email list.'}, status=400)

    email_re = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    emails = [e.strip() for e in emails if isinstance(e, str) and e.strip()]

    if not emails:
        return JsonResponse({'status': 'error', 'message': 'At least 1 recipient email is required.'}, status=400)
    if len(emails) > 5:
        return JsonResponse({'status': 'error', 'message': 'Maximum 5 test recipients allowed.'}, status=400)
    invalid = [e for e in emails if not email_re.match(e)]
    if invalid:
        return JsonResponse(
            {'status': 'error', 'message': f'Invalid address(es): {", ".join(invalid)}'},
            status=400,
        )

    from Email_validate_app.services.campaign_sender import send_test_campaign_emails as _send_test_campaign_emails
    sent, errors = _send_test_campaign_emails(campaign, emails)
    status = 'success' if sent > 0 else 'failed'
    CampaignTestSend.objects.create(campaign=campaign, recipients=emails, status=status)

    if sent > 0:
        return JsonResponse({
            'status':  'ok',
            'sent':    sent,
            'message': f'Test email sent to {sent} recipient(s).',
        })
    return JsonResponse(
        {'status': 'error', 'message': 'Failed to send: ' + '; '.join(errors)},
        status=500,
    )


def campaign_unsubscribe(request, token):
    from django.core import signing
    from django.utils import timezone
    from Email_validate_app.models import CampaignEmail, Campaign, CampaignEvent

    try:
        data = signing.loads(token, salt='unsub', max_age=86400 * 365)
        email       = data['e']
        campaign_id = data['c']
        is_test     = data.get('test', False)
    except Exception:
        return HttpResponse('Invalid or expired unsubscribe link.', status=400, content_type='text/plain')

    if is_test:
        return HttpResponse('You have been unsubscribed.', content_type='text/plain')

    try:
        campaign = Campaign.objects.get(id=campaign_id, deleted_at__isnull=True)
    except Campaign.DoesNotExist:
        return HttpResponse('Invalid unsubscribe link.', status=400, content_type='text/plain')

    contact = CampaignEmail.objects.filter(
        list_id=campaign.campaign_list_id,
        email=email,
        deleted_at__isnull=True,
    ).first()

    if contact and contact.subscribed != 'unsubscribed':
        contact.subscribed = 'unsubscribed'
        contact.save(update_fields=['subscribed'])

        CampaignEvent.objects.get_or_create(
            campaign=campaign,
            email=email,
            event_type='unsubscribe',
            defaults={
                'message_id': f'unsub-{campaign_id}-{email}',
                'event_time': timezone.now(),
                'raw_payload': {},
            },
        )

        from Email_validate_app.tasks.cloudwatch_sync import _recalculate_stats
        from Email_validate_app.models import CampaignStats
        stats, _ = CampaignStats.objects.get_or_create(campaign=campaign)
        _recalculate_stats(campaign, stats)

        if campaign.campaign_list_id:
            _sync_campaign_list_counts(campaign.campaign_list_id)

    return HttpResponse('You have been unsubscribed.', content_type='text/plain')


def create_campaign(request):
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    from Email_validate_app.models import CampaignList, TemplateLibrary, UserTemplate, Campaign, SenderEmailToken, Segment
    user_id = get_user_id(request)

    from Email_validate_app.services.segment_builder import count_segment_contacts
    campaign_lists    = CampaignList.objects.filter(user_id=user_id, status='active', deleted_at__isnull=True).order_by('list_name')
    active_segments   = list(Segment.objects.filter(user_id=user_id, status='active', deleted_at__isnull=True).order_by('name'))
    for seg in active_segments:
        seg.contact_count = count_segment_contacts(seg, user_id)
    user_templates    = UserTemplate.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-updated_at')
    library_templates = TemplateLibrary.objects.filter(is_active=True, deleted_at__isnull=True).order_by('name')

    def _thumb_url(obj):
        try:
            return obj.thumbnail.url if obj.thumbnail else ''
        except ValueError:
            return ''

    user_templates_data = [{
        'id': t.id, 'name': t.name, 'subject': t.subject,
        'html_content': t.html_content, 'design_json': t.design_json,
        'thumbnail': _thumb_url(t),
    } for t in user_templates]

    library_templates_data = [{
        'id': t.id, 'name': t.name, 'subject': t.subject, 'category': t.category,
        'html_content': t.html_content, 'thumbnail': _thumb_url(t),
    } for t in library_templates]

    editing_campaign = None
    campaign_id = request.GET.get('campaign_id', '').strip()
    if campaign_id:
        try:
            camp = Campaign.objects.get(Campaign_ID=campaign_id, user_id=user_id, deleted_at__isnull=True)
        except Campaign.DoesNotExist:
            messages.warning(request, "Campaign not found.")
            return redirect(reverse('campaigns'))
        if camp.status not in ('draft', 'scheduled', 'failed'):
            messages.warning(request, "Only draft, scheduled, or failed campaigns can be edited.")
            return redirect(reverse('campaigns'))

        editing_campaign = {
            'id':                   camp.Campaign_ID,
            'status':               camp.status,
            'campaign_name':        camp.campaign_name,
            'campaign_list_id':     camp.campaign_list_id,
            'campaign_list_name':   camp.campaign_list.list_name if camp.campaign_list else '',
            'campaign_segment_id':  camp.campaign_segment_id,
            'campaign_segment_name': camp.campaign_segment.name if camp.campaign_segment else '',
            'list_ids':            list(camp.campaign_lists.values_list('id', flat=True)),
            'segment_ids':         list(camp.campaign_segments.values_list('id', flat=True)),
            'exclude_list_ids':    list(camp.exclude_lists.values_list('id', flat=True)),
            'exclude_segment_ids': list(camp.exclude_segments.values_list('id', flat=True)),
            'sender_name':          camp.sender_name,
            'from_email':           camp.from_email,
            'reply_email':          camp.reply_email,
            'send_option':          'schedule' if camp.schedule_at else 'now',
            'schedule_at':          camp.schedule_at.isoformat() if camp.schedule_at else '',
            'schedule_timezone':    camp.schedule_timezone or 'Asia/Kolkata',
            'template': ({
                'id': camp.template.id, 'name': camp.template.name,
                'subject': camp.template.subject, 'html_content': camp.template.html_content,
                'design_json': camp.template.design_json,
            } if camp.template else None),
        }

    verified_emails = list(
        SenderEmailToken.objects.filter(
            user_id=user_id, confirmed=True, is_hidden=False
        ).values_list('email', flat=True)
    )

    return render(request, 'i_Create_Campaign.html', {
        'campaign_lists':          campaign_lists,
        'active_segments':         active_segments,
        'user_templates':          user_templates,
        'library_templates':       library_templates,
        'user_templates_data':     user_templates_data,
        'library_templates_data':  library_templates_data,
        'editing_campaign':        editing_campaign,
        'verified_emails':         verified_emails,
    })


@_require_POST
def estimate_recipients_api(request):
    """Return deduplicated net recipient count after include/exclude logic."""
    if not request.session.get('logged_in'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    from Email_validate_app.models import CampaignEmail, Segment
    from Email_validate_app.services.segment_builder import get_segment_emails

    user_id       = get_user_id(request)
    list_ids      = [x for x in request.POST.get('list_ids',          '').split(',') if x.strip()]
    seg_ids       = [x for x in request.POST.get('segment_ids',       '').split(',') if x.strip()]
    excl_list_ids = [x for x in request.POST.get('exclude_list_ids',  '').split(',') if x.strip()]
    excl_seg_ids  = [x for x in request.POST.get('exclude_segment_ids','').split(',') if x.strip()]

    email_set = set()

    if list_ids:
        email_set.update(
            CampaignEmail.objects.filter(
                list_id__in=list_ids, user_id=user_id,
                deleted_at__isnull=True, subscribed='subscribed',
            ).values_list('email', flat=True)
        )

    if seg_ids:
        for seg in Segment.objects.filter(id__in=seg_ids, user_id=user_id, deleted_at__isnull=True):
            seg_emails = get_segment_emails(seg, user_id)
            email_set.update(
                CampaignEmail.objects.filter(
                    user_id=user_id, email__in=seg_emails,
                    subscribed='subscribed', deleted_at__isnull=True,
                ).values_list('email', flat=True)
            )

    if email_set:
        exclude_set = set()
        if excl_list_ids:
            exclude_set.update(
                CampaignEmail.objects.filter(
                    list_id__in=excl_list_ids, user_id=user_id, deleted_at__isnull=True,
                ).values_list('email', flat=True)
            )
        if excl_seg_ids:
            for seg in Segment.objects.filter(id__in=excl_seg_ids, user_id=user_id, deleted_at__isnull=True):
                exclude_set.update(get_segment_emails(seg, user_id))
        email_set -= exclude_set

    return JsonResponse({'count': len(email_set)})
