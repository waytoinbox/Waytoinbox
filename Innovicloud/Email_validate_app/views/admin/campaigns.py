import logging

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from Email_validate_app.models import Campaign
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import campaign_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_campaigns(request):
    page_obj, params, total = campaign_service.get_campaign_list(request.GET)
    return render(request, 'admin/campaigns/list.html', {
        'page': 'campaigns',
        'page_obj': page_obj,
        'params': params,
        'total': total,
    })


@admin_required
@handle_admin_errors
def admin_campaign_detail(request, cid):
    ctx = campaign_service.get_campaign_detail(cid)
    ctx['page'] = 'campaigns'
    ctx['admin_pk'] = request._admin_user.pk
    return render(request, 'admin/campaigns/detail.html', ctx)


@admin_required
@handle_admin_errors
@require_POST
def admin_campaign_cancel(request, cid):
    try:
        campaign, old_status = campaign_service.cancel_campaign(cid)
    except Campaign.DoesNotExist:
        return json_error('Campaign not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='campaign.cancel', module='campaigns',
        target_type='campaign', target_id=cid,
        target_repr=campaign.campaign_name,
        old_value={'status': old_status}, new_value={'status': 'cancelled'},
    )
    return json_ok(message=f'Campaign "{campaign.campaign_name}" cancelled.')


@admin_required
@handle_admin_errors
@require_POST
def admin_campaign_archive(request, cid):
    try:
        campaign = campaign_service.archive_campaign(cid)
    except Campaign.DoesNotExist:
        return json_error('Campaign not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='campaign.archive', module='campaigns',
        target_type='campaign', target_id=cid,
        target_repr=campaign.campaign_name,
    )
    return json_ok(
        data={'redirect': '/wti-admin/campaigns/'},
        message=f'Campaign "{campaign.campaign_name}" archived.',
    )


@admin_required
@handle_admin_errors
@require_POST
def admin_campaign_duplicate(request, cid):
    try:
        src = Campaign.objects.get(pk=cid)
        new_campaign = campaign_service.duplicate_campaign(cid)
    except Campaign.DoesNotExist:
        return json_error('Campaign not found.', status=404)

    audit(
        request, action='campaign.duplicate', module='campaigns',
        target_type='campaign', target_id=new_campaign.pk,
        target_repr=new_campaign.campaign_name,
        old_value={'source_id': cid, 'source_name': src.campaign_name},
    )
    return json_ok(
        data={'redirect': f'/wti-admin/campaigns/{new_campaign.pk}/'},
        message=f'Duplicated as "{new_campaign.campaign_name}".',
    )


@admin_required
@handle_admin_errors
def admin_campaign_preview(request, cid):
    campaign = get_object_or_404(Campaign, pk=cid)
    html = ''
    if campaign.template:
        html = campaign.template.html_content or ''
    return HttpResponse(html or '<p style="font-family:sans-serif;padding:24px;">No template content.</p>')
